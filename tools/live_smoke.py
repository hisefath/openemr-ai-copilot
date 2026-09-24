#!/usr/bin/env python3
"""One real call down every external path, to catch request shapes the API rejects.

    ANTHROPIC_API_KEY=... VOYAGE_API_KEY=... python tools/live_smoke.py

WHY THIS EXISTS. On 2026-09-23 document extraction and the supervisor's routing decision had been returning
HTTP 400 for the whole of Week 2:

    output_config.type: Extra inputs are not permitted

`output_config` takes {"format": {...}} and both call sites passed the format object directly. Nothing caught
it, and nothing *could* have: the eval gate replays recorded responses and never makes a call — that is
deliberate, it is what makes a 5% threshold mean something instead of measuring sampling noise — and the
recordings carry real surface keys with fixture responses, so the hash looked healthy while the kwargs the app
assembles were malformed. 396 tests, 55 gated cases and a holdout all passed over a request the API rejects.

So the gate proves the agent's LOGIC is right against a frozen model, and this proves the agent's REQUESTS are
still ones the API accepts. Neither substitutes for the other, and only this one costs money — a few cents.

Run it before recording a demo, after changing any request-assembling code, and after an SDK bump. It asserts
that each call is ACCEPTED and returns a parseable result; it deliberately does not assert on content, because
that is the gate's job and model output is not stable enough to gate on here.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "agent"))
sys.path.insert(0, str(REPO / "evals" / "w2"))

PASS, FAIL = "  ok  ", "  FAIL"


async def check(name, coro, results):
    try:
        detail = await coro
        print(f"{PASS}  {name:38} {detail}")
        results.append(True)
    except Exception as e:
        print(f"{FAIL}  {name:38} {type(e).__name__}: {str(e)[:150]}")
        results.append(False)


async def run() -> int:
    import anthropic
    import fixtures
    from anthropic import transform_schema

    from copilot import extract, llm
    from copilot.config import Settings
    from copilot.deadline import Deadline
    from copilot.documents import read_pages
    from copilot.graph import RoutingDecision
    from copilot.schemas import DocumentType

    missing = [k for k in ("ANTHROPIC_API_KEY", "VOYAGE_API_KEY") if not os.environ.get(k)]
    if missing:
        print(f"not set: {', '.join(missing)}", file=sys.stderr)
        return 2

    settings = Settings.from_env()
    client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"], max_retries=1)
    results: list = []
    print(f"live smoke against {settings.anthropic_model}\n")

    async def vision():
        """extract.py — the shape that was broken. A lab PDF through the real vision path."""
        pages = read_pages(fixtures.get("lab_abnormal"))
        seen, meta = await extract.read_document(client, settings, DocumentType.lab_pdf, pages.images,
                                                 Deadline(90.0))
        if seen is None:
            raise RuntimeError(f"no parsed document (reason={meta.reason}, http={meta.http_status})")
        return f"{len(seen.results)} results read"

    async def routing():
        """graph.py — the supervisor's structured routing call, same output_config shape."""
        resp = await client.messages.create(
            model=settings.anthropic_model, max_tokens=200,
            messages=[{"role": "user", "content": "Decide the next step for a clinical co-pilot answering a "
                                                  "follow-up question.\nState: {\"evidence_held\": 0}\n"
                                                  "Choose `retrieve` only if the held evidence cannot support "
                                                  "the new question."}],
            output_config={"format": {"type": "json_schema", "schema": transform_schema(RoutingDecision)}})
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        return f"next={RoutingDecision.model_validate_json(text).next.value}"

    async def answer_plan():
        """llm.py — the Week 1 answer path, included because a shared SDK bump breaks all three at once."""
        resp = await client.messages.create(
            model=settings.anthropic_model, max_tokens=400,
            messages=[{"role": "user", "content": "Say the patient has a penicillin allergy."}],
            output_config={"format": llm.PLAN_FORMAT})
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        if not text:
            raise RuntimeError(f"empty response (stop_reason={resp.stop_reason})")
        return f"{len(text)} chars of plan JSON"

    async def voyage():
        """retrieve.py — embedding and reranking, the other paid dependency the gate replays."""
        from copilot.retrieve import VoyageProvider
        p = VoyageProvider(api_key=os.environ["VOYAGE_API_KEY"])
        dims = len(await asyncio.to_thread(lambda: p.embed(["penicillin allergy"], input_type="query")[0]))
        ranked = await asyncio.to_thread(
            lambda: p.rerank("penicillin allergy",
                             ["Penicillin allergy contraindicates amoxicillin.", "Blood pressure targets."]))
        if ranked[0][0] != 0:
            raise RuntimeError(f"reranker put the irrelevant chunk first: {ranked}")
        return f"embed {dims}d, rerank top={ranked[0][1]:.3f}"

    await check("anthropic: vision extraction", vision(), results)
    await check("anthropic: supervisor routing", routing(), results)
    await check("anthropic: answer plan", answer_plan(), results)
    await check("voyage: embed + rerank", voyage(), results)

    failed = results.count(False)
    print(f"\n  {len(results) - failed}/{len(results)} live paths accepted")
    if failed:
        print("  A path the API rejects will fail in the deployed app while every offline test stays green.",
              file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
