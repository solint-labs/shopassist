"""Index knowledge into Qdrant.

    python -m scripts.ingest                       # markdown from KNOWLEDGE_DIR
    python -m scripts.ingest --shopify             # + Shopify policies, pages and products
    python -m scripts.ingest --query "do you ship to Dubai?"   # sanity-check retrieval

Safe to run on a schedule: unchanged documents are skipped (content hashes).
"""

from __future__ import annotations

import argparse
import asyncio

from app.agent.tenant import load_tenant_config
from app.logging_config import configure_logging
from app.main import build_qdrant, build_shopify
from app.rag.encoders import build_encoders
from app.rag.ingest import Ingestor, load_markdown_dir, shopify_documents
from app.rag.retriever import Retriever
from app.rag.store import KnowledgeStore
from app.settings import get_settings


async def main(with_shopify: bool, query: str | None) -> None:
    s = get_settings()
    configure_logging(s.log_level)
    tenant = load_tenant_config(s.tenant_config_path)
    dense, sparse, reranker = build_encoders(s.rag_encoders, s.rag_dense_model, s.rag_sparse_model,
                                             s.rag_reranker_model, s.rag_model_cache)
    store = KnowledgeStore(build_qdrant(s), dense, sparse)
    docs = load_markdown_dir(s.knowledge_dir)
    if with_shopify:
        shopify = build_shopify(s)
        if not shopify:
            raise SystemExit("SHOPIFY_SHOP is not configured")
        docs += await shopify_documents(shopify)
        await shopify.aclose()
    print(await Ingestor(store).ingest(tenant.tenant_id, docs))
    if query:
        r = await Retriever(store, reranker, None, s.rag_top_k, s.rag_top_n, s.rag_min_score, False).retrieve(
            tenant.tenant_id, query)
        for h in r.hits:
            print(f"{h.rerank_score:.3f}  {h.section}\n       {h.text[:140]!r}")
        if not r.found:
            print("No passage cleared the relevance threshold.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--shopify", action="store_true")
    ap.add_argument("--query")
    a = ap.parse_args()
    asyncio.run(main(a.shopify, a.query))
