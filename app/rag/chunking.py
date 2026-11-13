"""Heading-aware chunking for Markdown knowledge.

Support content is naturally structured (Shipping > International > Customs). Splitting
on headings keeps each chunk about one topic, and we prepend the heading path to the
chunk text ("contextual chunk header") so a chunk that just says "Allow 7-10 days" still
carries the information that it's about *international* shipping.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


@dataclass(slots=True)
class Document:
    doc_id: str  # stable id, e.g. "md:shipping" or "shopify:policy:REFUND_POLICY"
    title: str
    text: str
    url: str | None = None
    source: str = "markdown"  # markdown | shopify_policy | shopify_page | shopify_product
    metadata: dict = field(default_factory=dict)


@dataclass(slots=True)
class Chunk:
    doc_id: str
    index: int
    text: str  # the text that gets embedded (includes the heading path)
    title: str
    section: str
    url: str | None
    source: str
    content_hash: str
    metadata: dict = field(default_factory=dict)


def chunk_markdown(doc: Document, max_chars: int = 1200, overlap_chars: int = 150) -> list[Chunk]:
    sections = _split_sections(doc.text, doc.title)
    chunks: list[Chunk] = []
    for path, body in sections:
        for piece in _pack_paragraphs(body, max_chars, overlap_chars):
            header = " > ".join(path)
            text = f"{header}\n{piece}".strip()
            chunks.append(Chunk(
                doc_id=doc.doc_id, index=len(chunks), text=text, title=doc.title, section=header,
                url=doc.url, source=doc.source, content_hash=hashlib.sha256(text.encode()).hexdigest()[:16],
                metadata=dict(doc.metadata),
            ))
    return chunks


def _split_sections(text: str, title: str) -> list[tuple[list[str], str]]:
    """Return (heading_path, body) pairs. Content before the first heading uses the title."""
    path: list[tuple[int, str]] = []
    out: list[tuple[list[str], str]] = []
    buf: list[str] = []

    def flush() -> None:
        body = "\n".join(buf).strip()
        if body:
            names = [h for _, h in path] or [title]
            if names[0] != title:
                names = [title, *names]
            out.append((names, body))
        buf.clear()

    for line in text.splitlines():
        m = _HEADING.match(line)
        if m:
            flush()
            level, name = len(m.group(1)), m.group(2).strip()
            path = [(lvl, h) for lvl, h in path if lvl < level] + [(level, name)]
        else:
            buf.append(line)
    flush()
    return out


def _pack_paragraphs(body: str, max_chars: int, overlap_chars: int) -> list[str]:
    """Greedily pack paragraphs up to max_chars; overlap the tail of the previous chunk."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    pieces: list[str] = []
    cur = ""
    for p in paras:
        if len(p) > max_chars:  # very long paragraph: hard-split on sentences
            for s in re.split(r"(?<=[.!?])\s+", p):
                cur = _append(cur, s, max_chars, overlap_chars, pieces, sep=" ")
            continue
        cur = _append(cur, p, max_chars, overlap_chars, pieces, sep="\n\n")
    if cur:
        pieces.append(cur)
    return pieces


def _append(cur: str, part: str, max_chars: int, overlap: int, pieces: list[str], sep: str) -> str:
    if not cur:
        return part
    if len(cur) + len(sep) + len(part) <= max_chars:
        return cur + sep + part
    pieces.append(cur)
    tail = cur[-overlap:].split(" ", 1)[-1] if overlap else ""
    return (tail + sep + part).strip() if tail else part
