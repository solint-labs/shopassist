"""Knowledge ingestion: Markdown files and Shopify content -> Qdrant.

Sources
  * `knowledge/<tenant>/*.md`  - help-center articles maintained by the support team
  * Shopify shop policies       - refund, shipping, privacy, terms (always current)
  * Shopify pages               - FAQ / About / Wholesale pages from the online store
  * Shopify products            - descriptions, tags, variants (for "which beans are fruity?")

Ingestion is incremental: a document is re-embedded only when its chunk hashes change,
so a nightly cron (or a Shopify products/update webhook) is cheap to run.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

from app.commerce.shopify import ShopifyClient
from app.logging_config import log
from app.rag.chunking import Document, chunk_markdown
from app.rag.store import KnowledgeStore

_URL_HINT = re.compile(r"<!--\s*url:\s*(\S+)\s*-->")
_TAG = re.compile(r"<[^>]+>")


def html_to_text(s: str) -> str:
    s = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h[1-6]>", "\n", s or "")
    s = re.sub(r"(?i)<li[^>]*>", "- ", s)
    return html.unescape(_TAG.sub("", s)).strip()


def load_markdown_dir(path: str | Path) -> list[Document]:
    docs = []
    for f in sorted(Path(path).glob("**/*.md")):
        text = f.read_text(encoding="utf-8")
        m = re.search(r"^#\s+(.+)$", text, re.M)
        url = _URL_HINT.search(text)
        docs.append(Document(doc_id=f"md:{f.relative_to(path).with_suffix('')}", title=m.group(1).strip() if m else f.stem,
                             text=_URL_HINT.sub("", text), url=url.group(1) if url else None, source="markdown"))
    return docs


async def shopify_documents(client: ShopifyClient, include_products: bool = True) -> list[Document]:
    docs: list[Document] = []
    for p in await client.shop_policies():
        if p.get("body"):
            docs.append(Document(doc_id=f"shopify:policy:{p['type']}", title=p["title"], text=html_to_text(p["body"]),
                                 url=p.get("url"), source="shopify_policy"))
    try:
        for pg in await client.pages():
            docs.append(Document(doc_id=f"shopify:page:{pg['handle']}", title=pg["title"],
                                 text=html_to_text(pg.get("body", "")), source="shopify_page"))
    except Exception as e:  # noqa: BLE001 - pages need read_online_store_pages; optional
        log.warning("ingest.pages_skipped", error=str(e))
    if include_products:
        for pr in await client.all_products():
            variants = ", ".join(f"{v['title']} ({v['price']})" for v in pr.get("variants", {}).get("nodes", []))
            text = (f"{html_to_text(pr.get('descriptionHtml', ''))}\n\nType: {pr.get('productType') or '-'}\n"
                    f"Tags: {', '.join(pr.get('tags') or [])}\nVariants: {variants}")
            docs.append(Document(doc_id=f"shopify:product:{pr['handle']}", title=pr["title"], text=text,
                                 url=pr.get("onlineStoreUrl"), source="shopify_product"))
    return docs


class Ingestor:
    def __init__(self, store: KnowledgeStore, max_chars: int = 1200, overlap: int = 150):
        self.store, self.max_chars, self.overlap = store, max_chars, overlap

    async def ingest(self, tenant_id: str, docs: list[Document]) -> dict[str, int]:
        stats = {"documents": len(docs), "updated": 0, "unchanged": 0, "chunks": 0}
        for doc in docs:
            chunks = chunk_markdown(doc, self.max_chars, self.overlap)
            if await self.store.existing_hashes(tenant_id, doc.doc_id) == [c.content_hash for c in chunks]:
                stats["unchanged"] += 1
                continue
            await self.store.delete_document(tenant_id, doc.doc_id)  # drop stale chunks first
            stats["chunks"] += await self.store.upsert(tenant_id, chunks)
            stats["updated"] += 1
        log.info("ingest.done", tenant=tenant_id, **stats)
        return stats
