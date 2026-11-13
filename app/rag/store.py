"""Qdrant-backed knowledge store with hybrid (dense + sparse) search.

One collection per tenant (`kb_<tenant>`), each point carrying two named vectors:
  "dense"  - semantic embedding
  "sparse" - BM25-style term weights (Qdrant applies IDF server-side via Modifier.IDF)

Search uses Qdrant's Query API: two prefetches (dense, sparse) fused with Reciprocal
Rank Fusion. RRF needs no score calibration between the two retrievers, which is why
it's the default choice for hybrid search.

Point ids are deterministic (uuid5 of doc_id + chunk index), so re-ingesting a document
overwrites its chunks in place, and `delete_document` removes stale ones.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from qdrant_client import AsyncQdrantClient, models

from app.rag.chunking import Chunk
from app.rag.encoders import DenseEncoder, SparseEncoder

_NS = uuid.UUID("6f1c3e1e-2b7a-4c55-9a51-5b0c1d7f0a11")


@dataclass(slots=True)
class Hit:
    text: str
    title: str
    section: str
    url: str | None
    source: str
    doc_id: str
    fused_score: float
    rerank_score: float = 0.0


class KnowledgeStore:
    def __init__(self, client: AsyncQdrantClient, dense: DenseEncoder, sparse: SparseEncoder):
        self.client = client
        self.dense = dense
        self.sparse = sparse

    @staticmethod
    def collection(tenant_id: str) -> str:
        return f"kb_{tenant_id}"

    async def ensure_collection(self, tenant_id: str) -> None:
        name = self.collection(tenant_id)
        if await self.client.collection_exists(name):
            return
        await self.client.create_collection(
            name,
            vectors_config={"dense": models.VectorParams(size=self.dense.dim, distance=models.Distance.COSINE)},
            sparse_vectors_config={"sparse": models.SparseVectorParams(modifier=models.Modifier.IDF)},
        )
        # Payload index so we can delete/filter by document quickly.
        await self.client.create_payload_index(name, "doc_id", models.PayloadSchemaType.KEYWORD)

    async def upsert(self, tenant_id: str, chunks: list[Chunk]) -> int:
        if not chunks:
            return 0
        await self.ensure_collection(tenant_id)
        texts = [c.text for c in chunks]
        dense = self.dense.embed_documents(texts)
        sparse = self.sparse.encode_documents(texts)
        points = [
            models.PointStruct(
                id=str(uuid.uuid5(_NS, f"{c.doc_id}#{c.index}")),
                vector={"dense": d, "sparse": models.SparseVector(indices=s.indices, values=s.values)},
                payload={"text": c.text, "title": c.title, "section": c.section, "url": c.url, "source": c.source,
                         "doc_id": c.doc_id, "chunk_index": c.index, "content_hash": c.content_hash, **c.metadata},
            )
            for c, d, s in zip(chunks, dense, sparse)
        ]
        await self.client.upsert(self.collection(tenant_id), points=points, wait=True)
        return len(points)

    async def delete_document(self, tenant_id: str, doc_id: str) -> None:
        name = self.collection(tenant_id)
        if not await self.client.collection_exists(name):
            return
        await self.client.delete(name, points_selector=models.FilterSelector(filter=models.Filter(
            must=[models.FieldCondition(key="doc_id", match=models.MatchValue(value=doc_id))])))

    async def existing_hashes(self, tenant_id: str, doc_id: str) -> list[str]:
        """Content hashes of a document's chunks, in order. Used to skip unchanged docs."""
        name = self.collection(tenant_id)
        if not await self.client.collection_exists(name):
            return []
        points, _ = await self.client.scroll(name, scroll_filter=models.Filter(must=[
            models.FieldCondition(key="doc_id", match=models.MatchValue(value=doc_id))]), limit=1000,
            with_payload=["content_hash", "chunk_index"])
        return [p.payload["content_hash"] for p in sorted(points, key=lambda p: p.payload["chunk_index"])]

    async def hybrid_search(self, tenant_id: str, query: str, limit: int = 20) -> list[Hit]:
        name = self.collection(tenant_id)
        if not await self.client.collection_exists(name):
            return []
        sq = self.sparse.encode_query(query)
        res = await self.client.query_points(
            name,
            prefetch=[
                models.Prefetch(query=self.dense.embed_query(query), using="dense", limit=limit),
                models.Prefetch(query=models.SparseVector(indices=sq.indices, values=sq.values), using="sparse",
                                limit=limit),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            limit=limit,
            with_payload=True,
        )
        return [Hit(text=p.payload["text"], title=p.payload.get("title", ""), section=p.payload.get("section", ""),
                    url=p.payload.get("url"), source=p.payload.get("source", ""), doc_id=p.payload["doc_id"],
                    fused_score=p.score) for p in res.points]
