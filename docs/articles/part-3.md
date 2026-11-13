# Part 3: Trust: Escalation, Functional Flows and Privacy

*Building a production LLM virtual agent for e-commerce support, Part 3 of 4*

> **Series:** Part 1: Foundations · Part 2: Grounded answers (RAG + Shopify) · **Part 3: Trust (escalation, flows, privacy)** · Part 4: Production (scale, enterprise, personality)
>
> **Code:** [github.com/solint-labs/shopassist](https://github.com/solint-labs/shopassist), `git checkout part-3`

Customers and support leads don't judge a support bot on its best answers. They judge it on its worst moments: the angry customer who had to ask three times for a human, the cancellation the bot "confirmed" but never performed, the phone number pasted into a public comment that stayed visible for a day.

This part makes the agent trustworthy in three ways:

1. **Escalation:** recognise the conversations it shouldn't handle, and hand them off with context.
2. **Functional flows:** do things (cancel, return, erase) with the reliability of software, not the creativity of a language model.
3. **Privacy:** keep personal data out of the LLM, the logs and the database, while the agent can still use it.

Here's the turn loop after this part:

```
inbound
  1. pseudonymise PII ──────────────── stored and sent to the LLM as <EMAIL_1>, <PHONE_1>
  2. human owns this conversation? ─── record it, mirror to the ticket, stay silent
  3. escalation rules ──────────────── safety · legal · human request · frustration · repeated failure
  4. "delete my data"? ─────────────── erasure flow
  5. active flow? ──────────────────── the flow handles the message (cancel / return)
  6. RAG + LLM ⇄ tools ─────────────── may signal: start a flow, or escalate
  7. track unresolved answers ──────── feeds rule 3 on the next turn
  8. rehydrate placeholders only on delivery, only for this customer
```

## Part A: Escalation

### The scenarios

The goal of an AI agent isn't to resolve every conversation. It's to resolve the ones it safely can, and hand off the rest *early*, with context. We define the scenarios explicitly, with priorities that map to helpdesk SLAs:

| Scenario | Signal | Priority |
|---|---|---|
| Safety | allergy, injury, glass, burn, food poisoning | urgent |
| Legal | lawyer, consumer court, fraud, chargeback | urgent |
| Human request | "let me talk to a real person", "agent", or tenant-specific phrases (e.g. Hinglish "insaan se baat") | normal |
| Frustration | negative lexicon, profanity, SHOUTING, "!!!", persistence words ("still", "third time"), repeating the same question | high |
| Repeated failure | two consecutive turns the bot couldn't ground or resolve | normal |
| High value | cancellation or refund above a configured amount | normal |
| Damaged item | needs photos and judgement | normal |
| Verification locked | too many failed identity checks | normal |
| Model requested | the LLM called `escalate_to_human` (complaints, exceptions, compensation) | as requested |

### Rules first, model second

The first five scenarios are detected by **deterministic rules that run before the LLM** (`app/escalation/detector.py`). They're cheap, testable and auditable, and a customer who mentions an allergic reaction should never depend on a model's judgement. The rules are tuned per tenant in YAML: keywords, frustration threshold, the failure count, business hours.

The human-request patterns are more careful than keyword matching. "Am I talking to a bot?" shouldn't escalate (the agent should just answer honestly), while "can I talk to a real person" should. The test suite pins both cases.

The long tail ("I want compensation for the delay", "can you make an exception?") goes to the LLM through an `escalate_to_human` tool. The tool doesn't hand off by itself. It sets a *signal* that the orchestrator acts on after the turn. Tools never send messages directly.

### Anatomy of a good hand-off

`HandoffService.escalate()` does four things:

1. **Tells the customer what happens next, honestly.** The in-hours and out-of-hours messages come from config, chosen with the tenant's business hours and time zone. Safety escalations start with "if anyone is unwell or hurt, please get medical help right away".
2. **Summarises for the human.** An LLM call turns the transcript into a 2-3 sentence summary plus order numbers and sentiment, so the customer never has to repeat themselves. It falls back to the last few messages if the call fails.
3. **Creates a ticket where the team already works.** There's a `Helpdesk` interface with Zendesk and signed-webhook implementations (the webhook one covers Freshdesk, Gorgias, Zapier, n8n...). A helpdesk outage never blocks the hand-off itself.
4. **Steps back.** The conversation's status becomes `human`, and from then on the bot records incoming messages and mirrors them to the ticket, but **never replies**.

### Channel mechanics

On **Messenger and Instagram**, Meta's handover protocol lets the bot pass the thread to the Page or Instagram inbox, so agents answer natively in Meta Business Suite. We call `pass_thread_control` with the inbox app ids Meta documents (`263902037430900` for the Page inbox, `1217981644879628` for the Instagram inbox). While the inbox owns the thread, customer messages arrive on the `standby` webhook field. When an agent marks the conversation done, a `pass_thread_control` event returns ownership to our app id and the bot resumes. (Pages using Conversation Routing can still pass and release thread control, but other handover features are disabled.)

**WhatsApp** has no handover protocol. Human agents reply through our **agent API**: list the queue, read the transcript, send a message, release back to the bot. The API enforces messaging windows. WhatsApp allows free-form replies only within 24 hours of the customer's last message. Messenger and Instagram allow human agents up to 7 days with the `HUMAN_AGENT` tag. Outside those windows the API returns `409`, so the console can offer a template instead of failing silently.

## Part B: Functional flows

### Why not let the LLM do it?

Cancelling an order is a *commitment*. It must check policy exactly, get explicit confirmation, call the right API **exactly once**, and report what actually happened. LLMs are excellent at recognising intent ("I don't want it anymore" means cancellation) and poor at being auditable state machines.

So the work is split:

- The **LLM recognises intent** and calls `start_order_cancellation` or `start_return_request`, which only work for orders verified in Part 2.
- A **flow** (plain Python, `app/flows/`) owns the steps, the policy, the confirmation and the API call.
- While a flow is active, messages go to the flow first. Customers can leave at any time ("never mind"), and anything the flow can't parse is re-asked with buttons (two retries, then it lets go politely).

Flow state lives in the conversation state, so flows survive restarts and work across workers (Part 4).

### Cancel order

```
start ─► re-read the order from Shopify (never trust the chat history)
      ─► already cancelled?          → say so
      ─► already fulfilled/roasted?  → explain, offer a return later
      ─► total above threshold?      → escalate (high_value)
      ─► confirm with buttons [Yes, cancel it] [No, keep it]
yes   ─► orderCancel(refundMethod: original payment, restock, notifyCustomer) → "Done ✅"
```

Buttons render natively: WhatsApp interactive reply buttons, Messenger and Instagram quick replies. A tap comes back as a payload (`yes`/`no`), so there's no ambiguity. Free-text "yeah go ahead" and "nahi" are understood too. The mutation uses the current `orderCancel` signature in API `2026-10`, with an explicit `refundMethod: {originalPaymentMethodsRefund: true}`. If Shopify rejects it, the flow escalates with the error instead of pretending it worked.

### Return request

Returns show why policy belongs in code. Brewline's rules: opened coffee can't be returned (it's food), unopened coffee within 14 days, unused equipment within 30 days, damaged items reported within 7 days get a free replacement. The help center *explains* these rules. The flow *enforces* them:

```python
# app/flows/orders.py
if reason == "damaged":
    reply = await ctx.escalate(EscalationDecision("damaged_item", "normal", ...))
    reply.text = "I'm sorry it arrived like that. Please send a photo of the item and the packaging here. " + reply.text
    return reply
if perishable and reason in ("opened", "unused_gear"):
    return AgentReply("Opened coffee can't be returned because it's a food product. ...")
window = policy.unopened_perishable_days if perishable else policy.equipment_days
if days > window: ...
```

Whether an item is perishable comes from the Shopify **product type** (with a title-keyword fallback), not from the model's guess. Eligible returns are created with `returnRequest`, which puts the return in `REQUESTED` status for the merchant to approve. The agent never approves its own refunds.

### Forget me

"Delete my data" starts a short flow: confirm, then erase the conversation and every vault entry tied to it, and tell the customer that order records are kept as the law requires. It's the customer-facing half of the GDPR handling below.

## Part C: Privacy and PII

### What goes where

Without care, one message like "I'm asha@example.com, call me on +91 98765 43210" ends up in your database, your logs, your error tracker, your LLM provider's logs and your helpdesk. The design goal is that **raw PII exists in exactly one place, encrypted**, and everything else sees placeholders:

```
customer:   "my email is asha@example.com, order 1042"
stored:     "my email is <EMAIL_1>, order 1042"           messages table
LLM sees:   "my email is <EMAIL_1>, order 1042"           the provider never gets raw PII
vault:      <EMAIL_1> → Fernet(asha@example.com)          pii_tokens table, encrypted
tool call:  get_order_status(email="<EMAIL_1>")  → rehydrated in code before Shopify is called
reply:      "...confirmation sent to <EMAIL_1>"  → rehydrated just before delivery to this customer
```

**Pseudonymise rather than mask**, because the agent still has to *use* the value: looking up an order by email. Masking would break that. Tokens are per conversation, and the same value always gets the same token, so the model can reason about "<EMAIL_1>" consistently.

### Detection

`app/privacy/pii.py` has two layers:

- **Deterministic recognisers** for structured identifiers: email, phone (with care not to match order numbers, PIN codes or tracking numbers), **Luhn-validated** payment cards, UPI handles, and Indian government IDs (Aadhaar in its grouped form, PAN). The test suite pins the edge cases, such as a 16-digit number that fails Luhn and a 12-digit Indian mobile that looks like an Aadhaar.
- **Optional NER** with Microsoft Presidio (spaCy-based) for names and addresses (`PII_NER=true`).

Detections are either **reversible** (email, phone, name, address get tokens) or **irreversible**. Card numbers, government IDs, payment handles and OTPs are replaced with "[card number removed]" and never stored at all. The customer also gets a gentle note not to share those in chat.

### The vault

`PIIVault` stores token→value mappings encrypted with **Fernet** (AES-128-CBC with HMAC-SHA256). `MultiFernet` gives key rotation: put the new key first, run `scripts/retention.py --rotate-keys`, then retire the old key. For GDPR lookups, each entry also stores a **blind index**, an HMAC of the normalised value. "Find every conversation that mentions asha@example.com" then works without storing or decrypting any email.

### Logs, comments, and the edges

- **Logs:** a structlog processor masks PII in *every* log field before it's written. Logs get copied everywhere and usually outlive conversations.
- **Public comments:** if a customer posts their phone number under a post, the agent **hides the comment** (it stays visible to its author only) and moves the conversation to DM. Spam and abusive comments are hidden quietly, without arguing in public. Placeholders are never rehydrated into public replies.
- **Helpdesk tickets** get the pseudonymised summary by default. Human agents see real details through the agent API, which Part 4 restricts by role and writes to the audit log.

An honest trade-off: on WhatsApp, the platform user id *is* the phone number, and we need it to reply. It's stored as the conversation's user id, and erasure requests match against it too.

### Data-subject rights and retention

Shopify requires every app to handle three **mandatory privacy webhooks**, signed with HMAC-SHA256 (base64) in `X-Shopify-Hmac-SHA256`:

- `customers/data_request`: export what we hold about a customer
- `customers/redact`: erase it, found through the blind index (email, phone) and WhatsApp user ids
- `shop/redact`: erase a store's data after it uninstalls

A daily retention job deletes messages after 90 days and vault entries once a conversation has been idle for 30 days (configurable). Deleting per conversation rather than per token keeps placeholder numbering consistent in live chats.

## Try it

```bash
git checkout part-3
pytest -q        # 82 tests: escalation scenarios, hand-off lifecycle, flows, PII, GDPR webhook, retention
```

The tests cover all of the above end to end. The cancellation test checks that **nothing reaches Shopify before the customer taps "Yes"**. The privacy test checks that the email never appears in what the LLM received or in the database, but does appear in the message the customer gets.

## What's next

Bree is now safe to put in front of customers on one store, on one server, with one LLM provider, at modest volume. **Part 4** makes her production-grade: a durable queue with workers, LLM failover, multiple stores per deployment, role-based access with an audit trail, observability, an eval suite as a deployment gate, and the personality layer: tone, voice experiments and per-customer preferences.
