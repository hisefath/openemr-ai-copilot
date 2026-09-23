"""Hybrid retrieval: keyword, vectors, rerank, floor.

The behaviours worth guarding are the ones that let an answer be grounded in the wrong thing — a reranker that
always returns something, a stale vector index, or a cache miss quietly becoming "no evidence".
"""
import json
from pathlib import Path

import pytest

from copilot.retrieve import (CachedProvider, Chunk, HybridRetriever, RetrievalUnavailable, bm25_ranking,
                              cosine, dense_ranking, load_corpus, reciprocal_rank_fusion, tokenize)
from copilot.schemas import SourceType

CHUNKS = [
    Chunk("a", "Penicillin allergy", "Cross-reactivity", "Amoxicillin is a penicillin-class antibiotic."),
    Chunk("b", "Hypertension", "ACE inhibitors", "Lisinopril is an ACE inhibitor used in hypertension."),
    Chunk("c", "Diabetes", "HbA1c", "HbA1c reflects average blood glucose over two to three months."),
]
VECTORS = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]


class StubEmbedder:
    def __init__(self, vec): self.vec = vec
    def embed(self, texts, *, input_type): return [self.vec for _ in texts]


class StubReranker:
    """Scores by position in a fixed list of (index, score) pairs."""
    def __init__(self, pairs): self.pairs, self.calls = pairs, []
    def rerank(self, query, documents):
        self.calls.append((query, list(documents)))
        return [(i, s) for i, s in self.pairs if i < len(documents)]


def retriever(embedder=None, reranker=None, vectors=VECTORS, chunks=CHUNKS):
    return HybridRetriever(chunks, vectors, embedder, reranker)


# ---------------------------------------------------------------- the shipped corpus

def test_the_shipped_corpus_loads_and_is_labelled_demo_material():
    """Guards: shipping something that reads as real clinical guidance, or an unparseable corpus at start-up."""
    chunks = load_corpus()
    assert len(chunks) >= 10
    assert len({c.chunk_id for c in chunks}) == len(chunks), "duplicate chunk ids"
    assert all(c.text.strip() and c.title.strip() for c in chunks)
    blob = json.loads(Path(load_corpus.__defaults__[0]).read_text())
    assert "NOT clinical guidance" in blob["_about"]


# ---------------------------------------------------------------- the pieces

def test_bm25_finds_the_exact_term():
    """Guards: the keyword half quietly doing nothing, leaving retrieval purely semantic."""
    assert CHUNKS[bm25_ranking("lisinopril", CHUNKS)[0]].chunk_id == "b"


def test_bm25_returns_nothing_when_no_term_matches():
    """Guards: a ranking that always returns every chunk, which makes the floor the only real filter."""
    assert bm25_ranking("zzzz nonexistent", CHUNKS) == []


def test_dense_finds_the_nearest_vector():
    """Guards: the semantic half being unable to answer a query that shares no words with the chunk."""
    assert dense_ranking([0.0, 1.0, 0.0], VECTORS)[0] == 1


def test_cosine_is_bounded_and_orthogonality_is_zero():
    assert cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    assert cosine([1, 0], [0, 1]) == pytest.approx(0.0)
    assert cosine([0, 0], [1, 0]) == 0.0          # no division by zero on an empty vector


def test_fusion_combines_by_rank_not_score():
    """Guards: averaging a BM25 score with a cosine similarity, which are not the same quantity."""
    assert reciprocal_rank_fusion([2, 0], [2, 1])[0] == 2      # top of both wins
    assert set(reciprocal_rank_fusion([0], [1])) == {0, 1}     # neither ranking is dropped


def test_tokenize_ignores_punctuation_and_case():
    assert tokenize("HbA1c, over 2-3 months.") == ["hba1c", "over", "2", "3", "months"]


# ---------------------------------------------------------------- the floor

def test_evidence_below_the_floor_is_not_returned():
    """Guards: THE reason a reranker is here. Top-k always returns k chunks whether or not any bear on the
    question, and an answer grounded in irrelevant evidence is worse than one that says there is none."""
    r = retriever(StubEmbedder([1.0, 0, 0]), StubReranker([(0, 0.10), (1, 0.05)]))
    assert r.search("penicillin", floor=0.35) == []


def test_only_chunks_above_the_floor_come_back_and_top_k_caps_them():
    r = retriever(StubEmbedder([1.0, 0, 0]), StubReranker([(0, 0.9), (1, 0.8), (2, 0.2)]))
    out = r.search("penicillin", top_k=2)
    assert [e.score for e in out] == [0.9, 0.8] and len(out) == 2


def test_results_come_back_most_relevant_first():
    r = retriever(StubEmbedder([1.0, 0, 0]), StubReranker([(0, 0.5), (1, 0.95)]))
    assert [e.score for e in r.search("x")] == [0.95, 0.5]


# ---------------------------------------------------------------- what comes back

def test_evidence_is_labelled_guideline_never_chart():
    """Guards: the merge the PRD forbids. Guideline text must never render as a fact about this patient."""
    r = retriever(StubEmbedder([1.0, 0, 0]), StubReranker([(0, 0.9)]))
    e = r.search("penicillin")[0]
    assert e.citation.source_type is SourceType.guideline
    assert e.citation.source_id == e.chunk_id and e.citation.bbox is None
    assert e.title and e.section and e.citation.page_or_section


# ---------------------------------------------------------------- degradation and failure

def test_an_embedder_outage_degrades_to_keyword_rather_than_failing():
    """Guards: a Voyage outage taking the whole answer down when BM25 could still ground it."""
    class Broken:
        def embed(self, texts, *, input_type): raise RuntimeError("voyage down")
    r = retriever(Broken(), StubReranker([(0, 0.9)]))
    assert r.search("penicillin amoxicillin")[0].chunk_id == "a"


def test_a_missing_reranker_is_an_error_not_an_empty_result():
    """Guards: 'no evidence' and 'the retriever was broken' looking identical to the answer model."""
    with pytest.raises(RetrievalUnavailable, match="no_reranker"):
        retriever(StubEmbedder([1.0, 0, 0]), None).search("penicillin")


def test_a_vector_index_out_of_sync_with_the_corpus_is_refused():
    """Guards: a stale index silently returning the wrong chunk for every query — evidence that is confidently
    about the wrong topic."""
    with pytest.raises(RetrievalUnavailable, match="vectors_out_of_sync"):
        HybridRetriever(CHUNKS, VECTORS[:2], None, StubReranker([]))


def test_an_empty_query_retrieves_nothing_without_calling_anything():
    stub = StubReranker([(0, 0.9)])
    assert retriever(StubEmbedder([1.0, 0, 0]), stub).search("   ") == []
    assert stub.calls == []


# ---------------------------------------------------------------- the offline cache

def test_a_cached_provider_replays_what_was_recorded(tmp_path):
    p = tmp_path / "cache.json"
    key = CachedProvider.rerank_key("q", ["doc one"])
    p.write_text(json.dumps({"embeddings": {"q": [0.0, 1.0, 0.0]}, "reranks": {key: [[0, 0.91]]}}))
    c = CachedProvider(p)
    assert c.embed(["q"], input_type="query") == [[0.0, 1.0, 0.0]]
    assert c.rerank("q", ["doc one"]) == [(0, 0.91)]


def test_a_cache_miss_is_a_hard_failure_not_an_empty_result(tmp_path):
    """Guards: THE gate property, applied to retrieval. If a changed corpus or a new query silently returned no
    evidence, every evidence case would pass for the wrong reason and the gate would be blind to it."""
    p = tmp_path / "cache.json"
    p.write_text(json.dumps({"embeddings": {}, "reranks": {}}))
    c = CachedProvider(p)
    with pytest.raises(RetrievalUnavailable, match="no cached embedding"):
        c.embed(["unseen query"], input_type="query")
    with pytest.raises(RetrievalUnavailable, match="no cached rerank"):
        c.rerank("unseen query", ["doc"])


def test_the_rerank_key_changes_when_the_corpus_changes(tmp_path):
    """Guards: a cache that keeps replaying old scores after the chunks were edited."""
    a = CachedProvider.rerank_key("q", ["one", "two"])
    assert a != CachedProvider.rerank_key("q", ["one", "two-edited"])
    assert a != CachedProvider.rerank_key("different", ["one", "two"])
