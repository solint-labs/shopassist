# Part 2: Grounded Answers with Open-Source RAG and Shopify

*Building a production LLM virtual agent for e-commerce support, Part 2 of 4*

> **Series:** Part 1: Foundations · **Part 2: Grounded answers (RAG + Shopify)** · Part 3: Trust (escalation, flows, privacy) · Part 4: Production (scale, enterprise, personality)
>
> **Code:** [github.com/solint-labs/shopassist](https://github.com/solint-labs/shopassist), `git checkout part-2`

At the end of Part 1, Bree could hold a pleasant conversation on three channels. She also answered policy questions from the model's general knowledge, which is how support bots end up promising refunds the store doesn't offer.

A support agent has exactly two sources of truth. The **knowledge base** covers policies, product facts and how-tos. **Live systems** cover orders, stock and prices. This part connects both, and treats the cases where neither has the answer as a first-class outcome rather than an invitation to improvise.

## Why naive RAG fails in support

The textbook pipeline is to embed the question, take the top 3 chunks, put them in the prompt, and answer. It fails in three predictable ways:

1. **Always-on context.** Top-k always returns *something*. Ask "do you sell oat milk?" and the model receives three loosely related chunks about milk-based coffee drinks, then confidently invents an answer.
2. **Vocabulary mismatch both ways.** Dense embeddings handle paraphrase ("my parcel hasn't come" ≈ "delivery delay") but blur exact tokens like "V60", "AeroPress" or a SKU. Keyword search is the reverse.
3. **Follow-ups.** "What about the decaf?" retrieves nothing useful on its own.

The pipeline below addresses each of them, using only open-source components.

```
history ─► condense follow-up ─► hybrid search in Qdrant (dense BGE + sparse BM25, fused with RRF)
        ─► cross-encoder rerank ─► relevance threshold ─► numbered <sources> in the prompt
        ─► LLM ⇄ tools (Shopify) ─► validate citations ─► strip them ─► append policy link
```

## Ingestion: chunks that know where they came from

Help-center content is already structured (*Shipping › International › Customs*), so we chunk **by heading** and prepend the heading path to every chunk. This is a "contextual chunk header". A chunk that only says "Allow 7-12 business days" is useless to a retriever. Prefixed with *Shipping > International shipping*, it's findable and unambiguous.

```python
# app/rag/chunking.py
header = " > ".join(path)                 # e.g. "Shipping > International shipping"
text = f"{header}\n{piece}".strip()       # what gets embedded
```

Long sections are packed paragraph by paragraph up to ~1,200 characters, with a small overlap. Every chunk carries a content hash. Re-ingestion compares hashes per document and only re-embeds what changed, so a nightly job (or a Shopify `products/update` webhook) stays cheap.

Sources:

- `knowledge/<tenant>/*.md`: articles the support team writes (shipping, returns, subscriptions, brewing guides, FAQ)
- **Shopify shop policies**: refund, shipping, privacy and terms, always current
- **Shopify pages and products**: descriptions, tags and variants, for questions like "which beans are fruity?"

## Hybrid retrieval in Qdrant

We use **Qdrant** (open source, Apache-2.0) with two named vectors per point:

- `dense`: **BAAI/bge-small-en-v1.5** via `fastembed`, a 384-dimensional embedding running on CPU through ONNX runtime, with no PyTorch or GPU needed
- `sparse`: **BM25** term weights (`Qdrant/bm25`), with Qdrant applying the IDF component server-side via `Modifier.IDF`

One query runs both retrievers and fuses the results with **Reciprocal Rank Fusion**. RRF combines rankings rather than raw scores, so we never have to calibrate cosine similarity against BM25 scores:

```python
# app/rag/store.py
res = await self.client.query_points(
    name,
    prefetch=[
        models.Prefetch(query=self.dense.embed_query(query), using="dense", limit=limit),
        models.Prefetch(query=models.SparseVector(indices=sq.indices, values=sq.values), using="sparse", limit=limit),
    ],
    query=models.FusionQuery(fusion=models.Fusion.RRF),
    limit=limit, with_payload=True,
)
```

Each tenant gets its own collection (`kb_<tenant>`), which Part 4 relies on for isolation. Qdrant also has an embedded mode (`:memory:` or a local path), which is what the tests use. Production points at a Qdrant server.

## Rerank, then threshold: making "I don't know" explicit

Hybrid search returns ~20 candidates. A **cross-encoder** (`BAAI/bge-reranker-base`) then reads each (question, passage) pair *together* and scores relevance. That's far more precise than comparing two independently computed vectors. We map its logits through a sigmoid to get a 0-1 score, keep the top 4, and **drop anything below a threshold**:

```python
# app/rag/retriever.py
ranked = sorted(candidates, key=lambda h: h.rerank_score, reverse=True)
kept = [h for h in ranked if h.rerank_score >= self.min_score][: self.top_n]
```

The threshold is what makes the pipeline reliable. When nothing clears it, the prompt doesn't get weak context. It gets an explicit instruction instead:

> Knowledge base: no articles matched this message. Do NOT guess store policies, prices or product facts. If the customer needs that kind of information, say you're not sure and offer to connect them with the team.

"Nothing relevant" becomes a state the agent can act on. Part 3 counts these states and escalates after two in a row. Calibrate the threshold with the eval set from Part 4 rather than by intuition. Start around 0.3 and adjust based on false "don't know"s versus hallucinations.

| Component | Default (open source) | Multilingual alternative |
|---|---|---|
| Dense embeddings | `BAAI/bge-small-en-v1.5` | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` |
| Sparse | `Qdrant/bm25` | same (language-agnostic tokens) or SPLADE |
| Reranker | `BAAI/bge-reranker-base` | `jinaai/jina-reranker-v2-base-multilingual` |
| Vector DB | Qdrant | Qdrant |

All of them are behind small interfaces (`DenseEncoder`, `SparseEncoder`, `Reranker`), so swapping one is a config change. The repo also includes deterministic hashing encoders for tests and air-gapped demos.

## Condensing follow-ups

Before retrieval, a cheap LLM call rewrites the latest message into a standalone query using the recent history ("what about the decaf?" becomes "decaf coffee international shipping time"). It's skipped on the first message, where there's nothing to resolve, and it fails open: if the call errors, we search with the raw message.

## Cite, validate, strip

The model gets numbered sources and must cite them like `[1]`. Citations are how we *check* grounding, but "[2]" looks odd on WhatsApp. So `finalize_answer` validates them and then removes them:

- citations to non-existent sources (`[3]` when there are two) mark the answer **ungrounded**, which is logged and counted, and feeds escalation in Part 3
- citation markers are stripped from the customer-facing text
- if a cited source has a public URL (a policy page), we append "More details: <url>" so the customer can read the full policy

Source text is wrapped in `<sources>` tags with an explicit note that it's reference material, never instructions. That's a cheap defence against prompt injection planted in documents.

## Shopify: live data through tools

Policies come from the knowledge base. Order status, tracking and stock come from **Shopify's Admin GraphQL API**. The repo targets the latest stable version at the time of writing (`2026-10`) and keeps it in a single setting, because Shopify releases a version every quarter and supports each one for at least a year.

### Authentication changed in 2026

If you've built Shopify integrations before, note the change. **Since 1 January 2026, new custom apps can't be created in the Shopify admin.** They're created in the **Dev Dashboard**, and an app working on stores in its own organization gets tokens through the **client credentials grant**:

```
POST https://{shop}.myshopify.com/admin/oauth/access_token
grant_type=client_credentials&client_id=...&client_secret=...
→ {"access_token": "shpat_...", "scope": "...", "expires_in": 86399}
```

Those tokens last about 24 hours, so the client caches them, refreshes five minutes before expiry, and uses a lock so concurrent requests don't stampede the token endpoint. Legacy `shpat_` tokens from apps created before 2026 keep working, so the client supports both.

### Throttling

The Admin GraphQL API is rate-limited by **query cost** (a leaky bucket), not request count. A throttled response carries `THROTTLED` in `errors[].extensions.code` and the bucket state in `extensions.cost.throttleStatus`. The client waits just long enough for the bucket to refill to the requested cost, then retries. Queries are kept deliberately small (`first: 1` for orders, `first: 5` for products) to stay cheap.

## Tool design: the model chooses, code decides what to reveal

The agent gets two tools: `get_order_status` and `search_products`. The tool *descriptions* tell the model when to use them. The tool *implementations* enforce everything that matters.

**Identity verification lives in code, never in the prompt.** A prompt that says "only reveal orders after verifying the email" can be talked around. Code can't:

```python
# app/commerce/tools.py
def is_verified_for_order(order, msg, email, phone) -> bool:
    order_phones = [order.get("phone"), (order.get("shippingAddress") or {}).get("phone")]
    # WhatsApp gives us the customer's verified phone number for free.
    if msg.channel == Channel.WHATSAPP and any(phones_match(msg.user_id, p) for p in order_phones):
        return True
    if email and order.get("email") and email.strip().lower() == order["email"].strip().lower():
        return True
    ...
```

Three details make this robust:

- **Channel-aware verification.** On WhatsApp the sender *is* a phone number, so matching it against the order's phone verifies the customer with zero friction. On Messenger and Instagram, the customer must provide the checkout email.
- **No enumeration.** "Order doesn't exist" and "details don't match" return the *identical* response, so the tool can't be used to discover valid order numbers. After five failures in a conversation, lookups lock and the agent offers a human.
- **Minimal results.** The tool returns order name, dates, statuses, items, city and tracking. No street address and no payment details. What the model never sees, it can't leak.

Verified orders are remembered in per-conversation state, so the customer doesn't re-verify on every question. Part 3's flows build on that state.

### The tool loop

The orchestrator runs a bounded loop: call the LLM with tools; if it requests tool calls, execute them and append the results; repeat. A cap (`MAX_TOOL_ROUNDS`, default 4) stops runaway loops. When it's hit, the model is told to answer with what it has. Tool failures never raise into the loop. They become `{"error": ...}` results the model can explain ("I can't reach our order system right now").

## Comments get grounded too

The Instagram comment triage from Part 1 now receives retrieved sources as well, so "is this available in decaf?" under a post gets a correct public answer. The Swiss Water decaf comes straight from the FAQ, not from the model's imagination.

## Try it

```bash
git checkout part-2
pytest -q                                       # 33 tests
python -m scripts.ingest --query "can I return opened coffee?"   # index + inspect retrieval scores
python -m scripts.ingest --shopify              # + policies, pages and products from your store
```

The ingest script prints rerank scores for the query, which is the quickest way to develop intuition for the threshold. `docs/shopify-setup.md` covers scopes, protected customer data access (needed to compare emails and phones) and the Dev Dashboard flow.

## What's still missing

Bree now answers from facts and looks up real orders. But ask her to *cancel* an order and the best she can do is describe the policy. Tell her you had an allergic reaction and she'll cheerfully cite the brewing guide. And every email address a customer types is now sitting in our database and in our LLM provider's logs.

**Part 3** deals with trust: when to hand off to a human (and how to do it gracefully on each channel), how to run transactional flows like cancellations and returns deterministically, and how to keep personal data out of the LLM, the logs and the database while the agent can still use it.
