#!/usr/bin/env python3
"""Record the corpus vectors and the rerank scores the offline gate replays.

    VOYAGE_API_KEY=... python tools/record_retrieval.py

Writes two files, both committed:

    agent/copilot/corpus/vectors.json   one embedding per chunk. Ships with the package, so start-up costs
                                        nothing and production never re-embeds a corpus that has not changed.
    evals/w2/retrieval_cache.json       query embeddings and rerank scores for the eval queries, so the gate
                                        scores retrieval with no network and no key.

Deliberate and rare, like re-recording model responses. Re-run when the corpus changes or an eval query is
added — and review the diff, because a change here changes what the agent can cite.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "agent"))

from copilot.retrieve import (CANDIDATES, CachedProvider, VoyageProvider, bm25_ranking,  # noqa: E402
                              dense_ranking, load_corpus, reciprocal_rank_fusion)

VECTORS_OUT = REPO / "agent" / "copilot" / "corpus" / "vectors.json"
CACHE_OUT = REPO / "evals" / "w2" / "retrieval_cache.json"

# The questions the eval cases ask. Every one needs its embedding and its rerank scores on record, or the gate
# fails that case loudly rather than scoring it against no evidence.
QUERIES = [
    "Is it safe to start amoxicillin?",
    "What changed since the last visit?",
    "Why might this patient's potassium be rising?",
    "What should I know about their blood pressure medication?",
    "Is this HbA1c due for a recheck?",
    "The patient has a new dry cough. Anything to consider?",
    "What does this creatinine result mean?",
    "Anything important in the scanned lab report?",
]


def with_retry(fn, *a, **kw):
    """Voyage's free tier is 3 RPM without a payment method, and it answers 429 rather than queueing. Back off
    and retry instead of failing the whole recording on one throttled call."""
    delay = 25.0
    for attempt in range(6):
        try:
            return fn(*a, **kw)
        except Exception as e:
            if "RateLimit" not in type(e).__name__ or attempt == 5:
                raise
            print(f"    rate limited; waiting {delay:.0f}s (attempt {attempt + 1}/5)")
            time.sleep(delay)
            delay *= 1.6
    raise RuntimeError("unreachable")


def main() -> int:
    key = os.environ.get("VOYAGE_API_KEY")
    if not key:
        print("VOYAGE_API_KEY is not set", file=sys.stderr)
        return 2

    chunks = load_corpus()
    voyage = VoyageProvider(key)

    print(f"embedding {len(chunks)} corpus chunks…")
    vectors = with_retry(voyage.embed, [c.searchable for c in chunks], input_type="document")
    VECTORS_OUT.write_text(json.dumps(
        {"model": "voyage-3-lite", "dim": len(vectors[0]),
         "chunk_ids": [c.chunk_id for c in chunks], "vectors": vectors}) + "\n")
    print(f"  wrote {VECTORS_OUT.relative_to(REPO)}  ({len(vectors)} × {len(vectors[0])})")

    # Voyage's free tier without a payment method allows 3 requests/minute, so batch what can be batched and
    # pace the rest. One-time cost; the outputs are committed.
    pace = float(os.environ.get("VOYAGE_PACE_S", "21"))
    print(f"embedding {len(QUERIES)} queries in one call…")
    time.sleep(pace)
    query_vectors = with_retry(voyage.embed, QUERIES, input_type="query")

    embeddings, reranks = {}, {}
    for q, qv in zip(QUERIES, query_vectors):
        embeddings[q] = qv
        time.sleep(pace)
        # Reproduce exactly what the retriever will send the reranker, so the cache key matches at replay time.
        fused = reciprocal_rank_fusion(bm25_ranking(q, chunks), dense_ranking(qv, vectors))[:CANDIDATES]
        docs = [chunks[i].text for i in fused]
        scores = with_retry(voyage.rerank, q, docs)
        reranks[CachedProvider.rerank_key(q, docs)] = [[i, s] for i, s in scores]
        best = max(scores, key=lambda p: p[1]) if scores else (None, 0.0)
        top = chunks[fused[best[0]]].chunk_id if best[0] is not None else "—"
        print(f"  {q[:44]:46} top={top:7} score={best[1]:.3f}")

    CACHE_OUT.parent.mkdir(parents=True, exist_ok=True)
    CACHE_OUT.write_text(json.dumps({"embeddings": embeddings, "reranks": reranks}) + "\n")
    print(f"  wrote {CACHE_OUT.relative_to(REPO)}  ({len(embeddings)} queries)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
