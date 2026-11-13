"""Query-time retrieval: condense -> hybrid search -> rerank -> threshold.

The threshold is what makes RAG *reliable* for support. Without it, the top-k chunks
are always passed to the model, and a question the knowledge base can't answer gets a
confident answer stitched from loosely related text. With it, "nothing relevant" is an
explicit state the agent can act on (say so, ask a clarifying question, escalate).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

from app.llm.base import ChatMessage, LLMClient
from app.logging_config import log
from app.rag.encoders import Reranker
from app.rag.store import Hit, KnowledgeStore


@dataclass(slots=True)
class Retrieval:
    query: str
    hits: list[Hit] = field(default_factory=list)  # reranked, above threshold, best first

    @property
    def found(self) -> bool:
        return bool(self.hits)

    @property
    def top_score(self) -> float:
        return self.hits[0].rerank_score if self.hits else 0.0


CONDENSE_PROMPT = """Rewrite the customer's latest message as a standalone search query for a store's
help-center, resolving references to earlier messages ("it", "that one", "the decaf").
Keep product names, numbers and key nouns. Return JSON: {"query": "<standalone query>"}."""


class Retriever:
    def __init__(self, store: KnowledgeStore, reranker: Reranker, llm: LLMClient | None = None,
                 top_k: int = 20, top_n: int = 4, min_score: float = 0.3, condense: bool = True):
        self.store, self.reranker, self.llm = store, reranker, llm
        self.top_k, self.top_n, self.min_score, self.condense = top_k, top_n, min_score, condense

    async def standalone_query(self, history: list[ChatMessage]) -> str:
        """Turn 'what about the decaf?' into 'decaf coffee shipping times'."""
        latest = history[-1].content if history else ""
        prior_user_turns = [m for m in history[:-1] if m.role == "user"]
        if not (self.condense and self.llm and prior_user_turns):
            return latest  # first message: nothing to resolve, skip the extra LLM call
        convo = "\n".join(f"{m.role}: {m.content}" for m in history[-6:])
        try:
            resp = await self.llm.chat(CONDENSE_PROMPT, [ChatMessage(role="user", content=convo)],
                                       json_mode=True, temperature=0, max_tokens=80)
            q = json.loads(resp.text[resp.text.find("{"):resp.text.rfind("}") + 1] or "{}").get("query", "")
            return q.strip() or latest
        except Exception as e:  # noqa: BLE001 - condensing is an optimisation, never a failure
            log.warning("rag.condense_failed", error=str(e))
            return latest

    async def retrieve(self, tenant_id: str, query: str) -> Retrieval:
        if not query.strip():
            return Retrieval(query=query)
        candidates = await self.store.hybrid_search(tenant_id, query, self.top_k)
        if not candidates:
            return Retrieval(query=query)
        # Cross-encoders are CPU-bound; keep the event loop free.
        scores = await asyncio.to_thread(self.reranker.score, query, [h.text for h in candidates])
        for h, s in zip(candidates, scores):
            h.rerank_score = float(s)
        ranked = sorted(candidates, key=lambda h: h.rerank_score, reverse=True)
        kept = [h for h in ranked if h.rerank_score >= self.min_score][: self.top_n]
        log.info("rag.retrieved", query=query, candidates=len(candidates), kept=len(kept),
                 top=round(ranked[0].rerank_score, 3))
        return Retrieval(query=query, hits=kept)
