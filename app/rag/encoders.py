"""Dense embeddings, sparse (lexical) vectors and reranking.

Production defaults are open-source models served on CPU through `fastembed` (ONNX
runtime, no PyTorch, no GPU):

    dense     BAAI/bge-small-en-v1.5        384-d, strong English retrieval quality for its size
    sparse    Qdrant/bm25                   classic BM25 term weights; Qdrant applies IDF
    reranker  BAAI/bge-reranker-base        cross-encoder, scores (query, passage) pairs jointly

For multilingual stores swap in e.g. `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`
and `jinaai/jina-reranker-v2-base-multilingual`.

Why hybrid? Dense vectors catch paraphrases ("my parcel hasn't come" ≈ "delivery delay");
sparse vectors nail exact tokens dense models blur (SKU codes, "V60", "AeroPress", order
words). Reranking then fixes the ordering using a model that reads query and passage together.

The `hashing` implementations below have no model downloads and are fully deterministic.
They are for tests and air-gapped demos, not for production quality.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Protocol

_TOKEN = re.compile(r"[a-z0-9]+")
STOPWORDS = frozenset(
    "a an the and or of to in on for with is are was were be been it its this that these those i you we they "
    "my your our me do does did can could would should will how what when where which who why if at by from "
    "as about into than then there here so not no yes please hi hello hey thanks thank".split()
)


def tokenize(text: str) -> list[str]:
    toks = []
    for t in _TOKEN.findall(text.lower()):
        if t in STOPWORDS:
            continue
        if len(t) > 4 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]  # crude plural folding: "grinders" -> "grinder"
        toks.append(t)
    return toks


@dataclass(slots=True)
class Sparse:
    indices: list[int]
    values: list[float]


class DenseEncoder(Protocol):
    dim: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


class SparseEncoder(Protocol):
    def encode_documents(self, texts: list[str]) -> list[Sparse]: ...
    def encode_query(self, text: str) -> Sparse: ...


class Reranker(Protocol):
    def score(self, query: str, passages: list[str]) -> list[float]:
        """Relevance in [0, 1] for each passage (higher is better)."""
        ...


# ── fastembed (production) ────────────────────────────────────────────────────


class FastEmbedDense:
    def __init__(self, model: str = "BAAI/bge-small-en-v1.5", cache_dir: str | None = None):
        from fastembed import TextEmbedding

        self._m = TextEmbedding(model_name=model, cache_dir=cache_dir)
        self.dim = len(next(iter(self._m.embed(["dimension probe"]))))

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [v.tolist() for v in self._m.passage_embed(texts)]

    def embed_query(self, text: str) -> list[float]:
        return next(iter(self._m.query_embed(text))).tolist()


class FastEmbedSparse:
    def __init__(self, model: str = "Qdrant/bm25", cache_dir: str | None = None):
        from fastembed import SparseTextEmbedding

        self._m = SparseTextEmbedding(model_name=model, cache_dir=cache_dir)

    def encode_documents(self, texts: list[str]) -> list[Sparse]:
        return [Sparse(e.indices.tolist(), e.values.tolist()) for e in self._m.embed(texts)]

    def encode_query(self, text: str) -> Sparse:
        e = next(iter(self._m.query_embed(text)))
        return Sparse(e.indices.tolist(), e.values.tolist())


class FastEmbedReranker:
    def __init__(self, model: str = "BAAI/bge-reranker-base", cache_dir: str | None = None):
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        self._m = TextCrossEncoder(model_name=model, cache_dir=cache_dir)

    def score(self, query: str, passages: list[str]) -> list[float]:
        # Cross-encoders return logits; a sigmoid maps them to a calibratable 0..1 score.
        return [1 / (1 + math.exp(-float(s))) for s in self._m.rerank(query, passages)]


# ── Hashing / lexical (tests, offline demos) ──────────────────────────────────


def _h(token: str, mod: int) -> int:
    return int(hashlib.md5(token.encode()).hexdigest()[:8], 16) % mod


class HashingDense:
    """Feature-hashed bag of unigrams + bigrams, L2-normalised. Deterministic, no downloads."""

    def __init__(self, dim: int = 512):
        self.dim = dim

    def _vec(self, text: str) -> list[float]:
        toks = tokenize(text)
        feats = toks + [f"{a}_{b}" for a, b in zip(toks, toks[1:])]
        v = [0.0] * self.dim
        for f in feats:
            v[_h(f, self.dim)] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


class HashingSparse:
    """Term-frequency sparse vectors; Qdrant's IDF modifier turns them into BM25-like scores."""

    MOD = 2**20

    def _enc(self, text: str) -> Sparse:
        tf = Counter(_h(t, self.MOD) for t in tokenize(text))
        idx = sorted(tf)
        return Sparse(idx, [1.0 + math.log(tf[i]) for i in idx])

    def encode_documents(self, texts: list[str]) -> list[Sparse]:
        return [self._enc(t) for t in texts]

    def encode_query(self, text: str) -> Sparse:
        return self._enc(text)


class LexicalReranker:
    """Share of query terms present in the passage. Crude, but monotonic and in [0, 1]."""

    def score(self, query: str, passages: list[str]) -> list[float]:
        q = set(tokenize(query))
        if not q:
            return [0.0] * len(passages)
        return [len(q & set(tokenize(p))) / len(q) for p in passages]


def build_encoders(kind: str, dense_model: str, sparse_model: str, reranker_model: str,
                   cache_dir: str | None = None) -> tuple[DenseEncoder, SparseEncoder, Reranker]:
    if kind == "fastembed":
        return (FastEmbedDense(dense_model, cache_dir), FastEmbedSparse(sparse_model, cache_dir),
                FastEmbedReranker(reranker_model, cache_dir))
    return HashingDense(), HashingSparse(), LexicalReranker()
