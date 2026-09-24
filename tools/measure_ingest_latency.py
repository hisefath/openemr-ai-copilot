#!/usr/bin/env python3
"""Measure how long ingesting a real document actually takes, with real vision calls.

    ANTHROPIC_API_KEY=... python tools/measure_ingest_latency.py [--runs 8]

KEY_METRICS.md #12 ("time to reviewed document") and W2_COST_AND_LATENCY.md §3 have both carried *Pending* for
one reason: every automated run of the ingest path uses a fake vision call that returns in microseconds, so the
sub-second figure it produces says nothing about the real thing. A latency budget whose dominant term is
estimated is a guess with a table around it.

This runs the real pipeline — render, real Anthropic vision call, locate, assemble — over the eval fixtures and
reports per-step p50/p95. It costs money, which is why it is a tool you run deliberately rather than a test.

WHAT IT DOES NOT MEASURE, and why that is stated rather than hidden: the OpenEMR upload round trip. That needs a
clinician's OAuth token, which cannot be obtained without a human at a login screen. W2_COST_AND_LATENCY.md
carries it separately as two round trips against the deployed instance. Everything here is the part that
dominates — the vision call is the bottleneck by an order of magnitude.

Re-run when the model, the prompt, or the page-rendering path changes. Numbers land in
evals/w2/results/ingest_latency.json and are quoted in KEY_METRICS.md and W2_COST_AND_LATENCY.md.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "agent"))
sys.path.insert(0, str(REPO / "evals" / "w2"))

OUT = REPO / "evals" / "w2" / "results" / "ingest_latency.json"

# One of each required document type, plus the degraded scan — the slow path, because a page with no text layer
# falls through to OCR. Measuring only the clean lab would flatter the number in exactly the case that matters.
DOCS = ["lab_abnormal", "intake_full", "lab_degraded"]


def pct(values, p):
    """p50/p95 without numpy. Nearest-rank, which is honest at n=8 where interpolation invents precision."""
    if not values:
        return None
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round(p / 100 * len(ordered) + 0.5)) - 1))
    return round(ordered[k], 3)


async def one_run(client, settings, doc_type, data, deadline_cls):
    """One full ingest, timed per step. Returns (timings, n_values, ok)."""
    from copilot import extract
    from copilot.documents import read_pages
    from copilot.schemas import DocumentRef

    t = {}
    start = time.monotonic()
    pages = read_pages(data)
    t["render_and_words"] = time.monotonic() - start

    vision_start = time.monotonic()
    seen, meta = await extract.read_document(client, settings, doc_type, pages.images,
                                             deadline_cls(90.0))
    t["vision_call"] = time.monotonic() - vision_start
    if seen is None:
        return t, 0, False

    locate_start = time.monotonic()
    doc = DocumentRef(document_id="latency-probe", doc_type=doc_type, content_hash="probe",
                      page_count=pages.page_count)
    result = extract.assemble(seen, doc, pages)
    t["locate_and_assemble"] = time.monotonic() - locate_start
    t["total"] = time.monotonic() - start

    values = getattr(result, "results", None)
    if values is None:  # intake form
        values = (list(result.medications) + list(result.allergies) + list(result.family_history)
                  + ([result.chief_concern] if result.chief_concern else []))
    return t, len(values), True


async def run(runs: int) -> int:
    import anthropic
    import fixtures
    from copilot.config import Settings
    from copilot.deadline import Deadline
    from copilot.schemas import DocumentType

    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("ANTHROPIC_API_KEY is not set", file=sys.stderr)
        return 2

    settings = Settings.from_env()
    client = anthropic.AsyncAnthropic(api_key=key, max_retries=2)
    doc_types = {"lab_abnormal": DocumentType.lab_pdf, "lab_degraded": DocumentType.lab_pdf,
                 "intake_full": DocumentType.intake_form}

    per_doc, all_totals, failures = {}, [], 0
    for name in DOCS:
        data = fixtures.get(name)
        steps, totals, counts = {}, [], []
        print(f"\n{name} ({doc_types[name].value}, {runs} runs)")
        for i in range(runs):
            t, n, ok = await one_run(client, settings, doc_types[name], data, Deadline)
            if not ok:
                failures += 1
                print(f"  run {i + 1}: FAILED (no parsed document)")
                continue
            for k, v in t.items():
                steps.setdefault(k, []).append(v)
            totals.append(t["total"])
            counts.append(n)
            print(f"  run {i + 1}: {t['total']:.2f}s total  "
                  f"(render {t['render_and_words']:.2f}  vision {t['vision_call']:.2f}  "
                  f"locate {t['locate_and_assemble']:.3f})  {n} values")
        all_totals += totals
        per_doc[name] = {
            "runs": len(totals),
            "values_extracted_median": statistics.median(counts) if counts else None,
            "steps": {k: {"p50": pct(v, 50), "p95": pct(v, 95), "min": round(min(v), 3), "max": round(max(v), 3)}
                      for k, v in steps.items()},
        }

    summary = {
        "model": settings.anthropic_model,
        "runs_per_document": runs,
        "documents": DOCS,
        "failures": failures,
        "end_to_end": {"p50": pct(all_totals, 50), "p95": pct(all_totals, 95),
                       "min": round(min(all_totals), 3) if all_totals else None,
                       "max": round(max(all_totals), 3) if all_totals else None,
                       "n": len(all_totals)},
        "per_document": per_doc,
        "_excludes": "OpenEMR upload round trip: needs a clinician OAuth token. See W2_COST_AND_LATENCY.md §3.",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(summary, indent=2) + "\n")

    e = summary["end_to_end"]
    print(f"\n{'=' * 64}\nEND TO END over {e['n']} real ingests (excl. OpenEMR upload)")
    print(f"  p50 {e['p50']}s   p95 {e['p95']}s   min {e['min']}s   max {e['max']}s")
    for name, d in per_doc.items():
        v = d["steps"].get("vision_call", {})
        print(f"  {name:14} vision p50 {v.get('p50')}s  p95 {v.get('p95')}s")
    print(f"\n  wrote {OUT.relative_to(REPO)}")
    if failures:
        print(f"  {failures} run(s) failed to parse — investigate before quoting these numbers", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=8, help="runs per document type (default 8)")
    raise SystemExit(asyncio.run(run(ap.parse_args().runs)))
