"""Retrieval-augmented generation, built entirely on open-source components.

    ingest:   markdown / Shopify policies, pages, products
              -> heading-aware chunks with contextual headers
              -> dense (BGE via fastembed) + sparse (BM25) vectors
              -> Qdrant collection (one per tenant)

    query:    condense follow-up into a standalone query
              -> hybrid search (dense + sparse, fused with RRF)
              -> cross-encoder rerank -> relevance threshold
              -> numbered sources in the prompt -> cited answer
              -> citation validation, then citations stripped for chat
"""
