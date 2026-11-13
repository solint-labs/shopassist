"""Part 2: chunking, hybrid retrieval with threshold, grounding."""

from pathlib import Path

import pytest
from qdrant_client import AsyncQdrantClient

from app.rag.chunking import Document, chunk_markdown
from app.rag.encoders import HashingDense, HashingSparse, LexicalReranker
from app.rag.grounding import finalize_answer, knowledge_section
from app.rag.ingest import Ingestor, load_markdown_dir
from app.rag.retriever import Retrieval, Retriever
from app.rag.store import Hit, KnowledgeStore

KB = Path(__file__).resolve().parents[1] / "knowledge/brewline"


def test_chunking_keeps_heading_path():
    doc = Document(doc_id="md:x", title="Shipping", text="# Shipping\n## International\nAllow 7-12 days.\n\n"
                                                         "## Tracking\nLink by email.")
    chunks = chunk_markdown(doc)
    assert [c.section for c in chunks] == ["Shipping > International", "Shipping > Tracking"]
    assert chunks[0].text.startswith("Shipping > International\nAllow 7-12 days.")


def test_chunking_splits_long_sections_with_overlap():
    body = "\n\n".join(f"Paragraph {i} " + "word " * 60 for i in range(10))
    chunks = chunk_markdown(Document(doc_id="d", title="T", text=body), max_chars=500, overlap_chars=80)
    assert len(chunks) > 3 and all(len(c.text) <= 600 for c in chunks)


@pytest.fixture
async def retriever():
    store = KnowledgeStore(AsyncQdrantClient(location=":memory:"), HashingDense(), HashingSparse())
    stats = await Ingestor(store).ingest("brewline", load_markdown_dir(KB))
    assert stats["updated"] == 5 and stats["chunks"] > 10
    return Retriever(store, LexicalReranker(), llm=None, min_score=0.5)


async def test_hybrid_retrieval_finds_the_right_section(retriever):
    r = await retriever.retrieve("brewline", "international shipping customs duties")
    assert r.found and "International shipping" in r.hits[0].section
    r = await retriever.retrieve("brewline", "how do I cancel my subscription")
    assert "Cancelling a subscription" in r.hits[0].section


async def test_out_of_scope_question_returns_nothing(retriever):
    r = await retriever.retrieve("brewline", "who won the cricket world cup final")
    assert not r.found  # the threshold turns "nothing relevant" into an explicit state


async def test_reingest_is_incremental(retriever):
    stats = await Ingestor(retriever.store).ingest("brewline", load_markdown_dir(KB))
    assert stats["updated"] == 0 and stats["unchanged"] == 5


def _retrieval():
    hits = [Hit(text="Opened bags cannot be returned.", title="Returns", section="Returns > Coffee", url="https://b.example/refund",
                source="markdown", doc_id="md:returns", fused_score=1, rerank_score=0.9)]
    return Retrieval(query="q", hits=hits)


def test_grounding_strips_citations_and_adds_link():
    a = finalize_answer("Opened bags can't be returned [1].", _retrieval())
    assert a.text.startswith("Opened bags can't be returned.") and "https://b.example/refund" in a.text
    assert a.cited == [1] and a.grounded


def test_grounding_flags_invented_citation():
    a = finalize_answer("Sure, returns are free [3].", _retrieval())
    assert a.invalid_citations == [3] and not a.grounded


def test_knowledge_section_without_hits_forbids_guessing():
    assert "Do NOT guess" in knowledge_section(Retrieval(query="x"))
    assert '<source id="1" title="Returns &gt; Coffee">' in knowledge_section(_retrieval())
