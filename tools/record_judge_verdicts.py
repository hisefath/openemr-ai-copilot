#!/usr/bin/env python3
"""Record the judge's verdict for every claim the eval set produces, so the gate can score them offline.

    ANTHROPIC_API_KEY=... python tools/record_judge_verdicts.py

Same discipline as the model recordings and the retrieval cache: the gate has no network and no key, so a rubric
that needed a live judge would put the most heavily graded element behind a paid API. Verdicts are keyed on a
hash of (system prompt, source, claim) — so editing the judge's prompt is a cache MISS, and a miss is a hard
failure rather than a silently-passing case.

Deliberate and rare. Re-run when the judge prompt or the eval documents change, and review the diff: a change
here changes what the gate considers factually consistent.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "agent"))
sys.path.insert(0, str(REPO / "evals" / "w2"))

import judge  # noqa: E402

OUT = REPO / "evals" / "w2" / "judge_verdicts.json"


def key(source: str, claim: str) -> str:
    h = hashlib.sha256(judge.SYSTEM.encode())
    h.update(b"\x00" + source.encode())
    h.update(b"\x00" + claim.encode())
    return h.hexdigest()[:16]


def claims_from_eval_set():
    """Every (source, claim) the eval documents produce: each extracted value, against the page it came from."""
    import fixtures
    from run_gate import CASES, load_cases
    from copilot.documents import read_pages

    page_text = {}
    seen, out = set(), []
    for case in load_cases(CASES):
        doc = case.get("document")
        if not doc:
            continue
        name = doc["fixture"]
        if name not in page_text:
            pages = read_pages(fixtures.get(name))
            page_text[name] = " ".join(w.text for words in pages.words for w in words)
        plan = doc["fixture_plan"]
        values = [r["value"] for r in plan.get("results", [])]
        values += [a["substance"] for a in plan.get("allergies", [])]
        values += [m["name"] for m in plan.get("medications", [])]
        values += [f["condition"] for f in plan.get("family_history", [])]
        if plan.get("chief_concern"):
            values.append(plan["chief_concern"]["value"])
        for v in values:
            k = key(page_text[name], v)
            if k not in seen:
                seen.add(k)
                out.append((page_text[name], v, k))
    return out


async def run() -> int:
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ANTHROPIC_API_KEY is not set", file=sys.stderr)
        return 2
    if not judge.is_trusted():
        print("the judge is not calibrated as trusted — run tools/calibrate_judge.py first", file=sys.stderr)
        return 2

    client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=2)
    model = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")
    items = claims_from_eval_set()
    print(f"judging {len(items)} distinct claims…")

    verdicts, cost, false_count = {}, 0.0, 0
    for source, claim, k in items:
        resp = await client.messages.create(
            model=model, max_tokens=8, system=judge.SYSTEM,
            messages=[{"role": "user", "content": judge.prompt(claim, source)}])
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        v = judge.parse(text)
        cost += resp.usage.input_tokens / 1e6 * 1.0 + resp.usage.output_tokens / 1e6 * 5.0
        verdicts[k] = {"claim": claim, "verdict": v}
        if v is not True:
            false_count += 1
            print(f"  NOT SUPPORTED: {claim[:48]}")

    OUT.write_text(json.dumps({"model": model, "system_prompt_sha": key("", "")[:8],
                               "verdicts": verdicts}, indent=2) + "\n")
    print(f"\n  {len(verdicts)} verdicts, {false_count} not supported")
    print(f"  wrote {OUT.relative_to(REPO)}   cost ~${round(cost, 5)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
