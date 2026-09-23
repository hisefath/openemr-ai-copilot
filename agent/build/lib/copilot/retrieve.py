"""Hybrid retrieval over the guideline corpus: keyword, vectors, then a reranker.

Three stages, because each fails differently:

  BM25    exact terms. Finds "lisinopril" when the query says lisinopril, and is blind to the query that says
          "blood pressure pill" instead.
  DENSE   meaning. Finds the ACE-inhibitor chunk from "blood pressure pill", and cheerfully returns something
          topically adjacent when nothing relevant exists.
  RERANK  a cross-encoder reads query and chunk together and scores the pair. This is what lets us apply a
          FLOOR rather than a top-k: "return the best three" always returns three, whether or not any of them
          bear on the question, and an answer grounded in irrelevant evidence is worse than one that says the
          corpus has nothing.

Candidates from the two retrievers are fused by reciprocal rank rather than by score, because a BM25 score and
a cosine similarity are not on the same scale and averaging them is meaningless.

EVERYTHING HERE MUST RUN OFFLINE. The eval gate has no network and no key, and a retriever that needs Voyage to
score a case would put the most heavily graded element behind a paid API. So embedding and reranking are
injected, exactly like the LLM at `app.state.llm`: Voyage in production, a cache in CI. A cache miss is a hard
failure, never a live call and never a silent empty result.
"""
from __future__ import annotations

import json
import logging
import math
import re
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional, Protocol, Sequence, Tuple

from .schemas import Citation, EvidenceChunk, SourceType

log = logging.getLogger("agent")

CORPUS = Path(__file__).parent / "corpus" / "guidelines.json"
CANDIDATES = 8          # per retriever, before fusion; the corpus is small, so this is generous
TOP_K = 3               # how many chunks may reach the answer model
SCORE_FLOOR = 0.50      # measured, not guessed — see below
# Where 0.50 comes from. Recording the eight eval queries against this corpus (tools/record_retrieval.py) gave
# top scores of 0.652, 0.695, 0.688, 0.660, 0.594, 0.523 and 0.516 for questions the corpus genuinely answers,
# and 0.475 for "What changed since the last visit?" — a question about the patient's own chart, which no
# guideline chunk should answer. A floor of 0.50 separates those two groups. At 0.35 that last query would have
# returned a potassium reference range as "evidence", which is exactly the irrelevant-but-confident grounding
# the reranker is here to prevent. Re-derive this if the corpus changes.
RRF_K = 60              # standard reciprocal-rank-fusion constant


class Chunk(NamedTuple):
    chunk_id: str
    title: str
    section: str
    text: str

    @property
    def searchable(self) -> str:
        return f"{self.title} {self.section} {self.text}"


class Embedder(Protocol):
    def embed(self, texts: Sequence[str], *, input_type: str) -> List[List[float]]: ...


class Reranker(Protocol):
    def rerank(self, query: str, documents: Sequence[str]) -> List[Tuple[int, float]]: ...


class RetrievalUnavailable(RuntimeError):
    """Retrieval could not run. Surfaced as `evidence_below_floor`, never as a silent empty list — an answer
    with no evidence and an answer whose retriever was broken must not look the same."""


def load_corpus(path: Path = CORPUS) -> List[Chunk]:
    data = json.loads(path.read_text())
    return [Chunk(c["chunk_id"], c["title"], c["section"], c["text"]) for c in data["chunks"]]


# ---------------------------------------------------------------- sparse

_TOKEN = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> List[str]:
    return _TOKEN.findall(text.lower())


def bm25_ranking(query: str, chunks: Sequence[Chunk], n: int = CANDIDATES) -> List[int]:
    from rank_bm25 import BM25Okapi

    scores = BM25Okapi([tokenize(c.searchable) for c in chunks]).get_scores(tokenize(query))
    ranked = sorted(range(len(chunks)), key=lambda i: scores[i], reverse=True)
    return [i for i in ranked if scores[i] > 0][:n]


# ---------------------------------------------------------------- dense


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    num = sum(x * y for x, y in zip(a, b))
    den = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return num / den if den else 0.0


def dense_ranking(query_vec: Sequence[float], vectors: Sequence[Sequence[float]],
                  n: int = CANDIDATES) -> List[int]:
    """Brute-force cosine. The corpus is fifteen chunks: a vector database here would be a service to run,
    secure and explain in exchange for nothing measurable."""
    sims = [cosine(query_vec, v) for v in vectors]
    return sorted(range(len(vectors)), key=lambda i: sims[i], reverse=True)[:n]


# ---------------------------------------------------------------- fusion


def reciprocal_rank_fusion(*rankings: Sequence[int], k: int = RRF_K) -> List[int]:
    """Combine rankings by position, not by score. A BM25 score and a cosine similarity are not comparable
    quantities; their ranks are."""
    points: Dict[int, float] = {}
    for ranking in rankings:
        for rank, idx in enumerate(ranking):
            points[idx] = points.get(idx, 0.0) + 1.0 / (k + rank + 1)
    return sorted(points, key=lambda i: points[i], reverse=True)


# ---------------------------------------------------------------- the retriever


class HybridRetriever:
    """One per process. Vectors are computed once and committed, so start-up costs nothing and CI needs no key."""

    def __init__(self, chunks: Sequence[Chunk], vectors: Optional[Sequence[Sequence[float]]],
                 embedder: Optional[Embedder], reranker: Optional[Reranker]):
        self._chunks, self._vectors = list(chunks), list(vectors or [])
        self._embedder, self._reranker = embedder, reranker
        if self._vectors and len(self._vectors) != len(self._chunks):
            raise RetrievalUnavailable("vectors_out_of_sync")   # a stale index silently returns the wrong chunk

    def search(self, query: str, *, top_k: int = TOP_K, floor: float = SCORE_FLOOR) -> List[EvidenceChunk]:
        """Evidence above the floor, most relevant first. Possibly empty — which is a real answer."""
        if not query.strip():
            return []
        sparse = bm25_ranking(query, self._chunks)

        dense: List[int] = []
        if self._embedder and self._vectors:
            try:
                qv = self._embedder.embed([query], input_type="query")[0]
                dense = dense_ranking(qv, self._vectors)
            except RetrievalUnavailable:
                raise
            except Exception as e:                     # an embedder outage degrades to keyword, it does not fail
                log.warning("dense_unavailable", extra={"error": type(e).__name__})

        fused = reciprocal_rank_fusion(sparse, dense)[:CANDIDATES]
        if not fused:
            return []

        if self._reranker is None:
            raise RetrievalUnavailable("no_reranker")
        try:
            scored = self._reranker.rerank(query, [self._chunks[i].text for i in fused])
        except RetrievalUnavailable:
            raise
        except Exception as e:
            # Degrading to "return the fused order unscored" would drop the floor, and the floor is the entire
            # reason a reranker is here: without it the answer gets three chunks whether or not any of them
            # bear on the question. Returning nothing and saying so is the safe direction.
            log.warning("reranker_unavailable", extra={"error": type(e).__name__})
            raise RetrievalUnavailable("reranker_unavailable") from None

        out: List[EvidenceChunk] = []
        for position, score in sorted(scored, key=lambda p: p[1], reverse=True):
            if score < floor or len(out) >= top_k:
                break
            c = self._chunks[fused[position]]
            out.append(EvidenceChunk(
                chunk_id=c.chunk_id, text=c.text, title=c.title, section=c.section, score=float(score),
                citation=Citation(source_type=SourceType.guideline, source_id=c.chunk_id,
                                  page_or_section=f"{c.title} — {c.section}", field_or_chunk_id=c.chunk_id,
                                  quote_or_value=c.text[:200], bbox=None),
            ))
        return out


# ---------------------------------------------------------------- providers


class VoyageProvider:
    """Production embedding and reranking. Imported lazily so nothing in CI needs the package or a key."""

    def __init__(self, api_key: str, embed_model: str = "voyage-3-lite", rerank_model: str = "rerank-2-lite"):
        import voyageai

        self._client = voyageai.Client(api_key=api_key)
        self._embed_model, self._rerank_model = embed_model, rerank_model

    def embed(self, texts: Sequence[str], *, input_type: str) -> List[List[float]]:
        return self._client.embed(list(texts), model=self._embed_model, input_type=input_type).embeddings

    def rerank(self, query: str, documents: Sequence[str]) -> List[Tuple[int, float]]:
        res = self._client.rerank(query, list(documents), model=self._rerank_model)
        return [(r.index, r.relevance_score) for r in res.results]


class CachedProvider:
    """Replays embeddings and rerank scores captured once, so the gate scores retrieval with no network.

    Same rule as the LLM seam: a miss is a hard failure. A retriever that silently returned nothing on a cache
    miss would make every evidence case pass for the wrong reason."""

    def __init__(self, path: Path):
        self._path = path
        blob = json.loads(path.read_text()) if path.exists() else {"embeddings": {}, "reranks": {}}
        self._embeddings: Dict[str, List[float]] = blob.get("embeddings", {})
        self._reranks: Dict[str, List[List[float]]] = blob.get("reranks", {})

    @staticmethod
    def rerank_key(query: str, documents: Sequence[str]) -> str:
        import hashlib
        h = hashlib.sha256(query.encode())
        for d in documents:
            h.update(b"\x00" + d.encode())
        return h.hexdigest()[:16]

    def embed(self, texts: Sequence[str], *, input_type: str) -> List[List[float]]:
        out = []
        for t in texts:
            if t not in self._embeddings:
                raise RetrievalUnavailable(
                    f"no cached embedding for {t!r} — re-record with tools/record_retrieval.py")
            out.append(self._embeddings[t])
        return out

    def rerank(self, query: str, documents: Sequence[str]) -> List[Tuple[int, float]]:
        key = self.rerank_key(query, documents)
        if key not in self._reranks:
            raise RetrievalUnavailable(
                f"no cached rerank for {query!r} — the corpus or the query changed; re-record deliberately")
        return [(int(i), float(s)) for i, s in self._reranks[key]]
