"""The replay layer's own check. If this passes, a grader's prompt edit turns the build red.

Runs under pytest (`pytest evals/w2`) or standalone (`python evals/w2/test_replay.py`).
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from anthropic.types import Message  # noqa: E402

from replay import CacheMiss, RecordingClient, ReplayClient, surface_key  # noqa: E402

PLAN = {"intent": "brief", "items": []}


def a_message() -> Message:
    return Message.model_validate({
        "id": "msg", "type": "message", "role": "assistant", "model": "claude-haiku-4-5",
        "content": [{"type": "text", "text": json.dumps(PLAN)}], "stop_reason": "end_turn",
        "stop_sequence": None, "usage": {"input_tokens": 1200, "output_tokens": 90},
    })


def kwargs(system: str = "You are a clinical co-pilot.", timeout: float = 8.4, model: str = "claude-haiku-4-5") -> dict:
    """Shaped like the dict llm.py:_call assembles."""
    return {
        "model": model, "max_tokens": 2048, "timeout": timeout,
        "messages": [{"role": "user", "content": "what changed?"}],
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "output_config": {"format": {"type": "json_schema", "schema": {}}},
    }


class _Live:
    """Stands in for anthropic.AsyncAnthropic during recording."""
    max_retries = 0

    def __init__(self) -> None:
        self.messages = self

    async def create(self, **_: object) -> Message:
        return a_message()


def _record(root: Path, case: str = "C01", **kw: object) -> None:
    rec = RecordingClient(_Live(), case, root=root)
    asyncio.run(rec.messages.create(**kwargs(**kw)))  # type: ignore[arg-type]
    rec.save()


def test_matching_surface_replays() -> None:
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _record(root)
        got = asyncio.run(ReplayClient("C01", root=root).messages.create(**kwargs()))
        assert json.loads(got.content[0].text) == PLAN


def test_timeout_does_not_affect_the_key() -> None:
    """`timeout` comes from deadline.remaining() and differs every run. If it were keyed, nothing would ever hit."""
    assert surface_key(kwargs(timeout=8.4)) == surface_key(kwargs(timeout=2.1))
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _record(root, timeout=8.4)
        got = asyncio.run(ReplayClient("C01", root=root).messages.create(**kwargs(timeout=0.3)))
        assert json.loads(got.content[0].text) == PLAN


def test_message_content_does_not_affect_the_key() -> None:
    """Case content is keyed by case id, not hashed — so editing a fixture does not invalidate every recording."""
    other = kwargs()
    other["messages"] = [{"role": "user", "content": "something else entirely"}]
    assert surface_key(kwargs()) == surface_key(other)


def test_edited_system_prompt_is_a_hard_failure() -> None:
    """THE one that matters: this is the regression a grader introduces."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _record(root, system="You are a clinical co-pilot.")
        client = ReplayClient("C01", root=root)
        try:
            asyncio.run(client.messages.create(**kwargs(system="You are a clinical co-pilot. Be terse.")))
        except CacheMiss as e:
            assert "surface changed" in str(e)
        else:
            raise AssertionError("an edited system prompt replayed silently — the gate is blind")


def test_swapped_model_is_a_hard_failure() -> None:
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _record(root, model="claude-haiku-4-5")
        client = ReplayClient("C01", root=root)
        try:
            asyncio.run(client.messages.create(**kwargs(model="claude-opus-5")))
        except CacheMiss:
            pass
        else:
            raise AssertionError("a swapped model replayed silently")


def test_extra_call_is_a_hard_failure() -> None:
    """The agent's control flow changed — one recorded call, two made."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _record(root)
        client = ReplayClient("C01", root=root)
        asyncio.run(client.messages.create(**kwargs()))
        try:
            asyncio.run(client.messages.create(**kwargs()))
        except CacheMiss as e:
            assert "only 1 were recorded" in str(e)
        else:
            raise AssertionError("an unrecorded extra call replayed silently")


def test_missing_recording_is_a_hard_failure() -> None:
    with tempfile.TemporaryDirectory() as d:
        try:
            ReplayClient("NOPE", root=Path(d))
        except CacheMiss:
            pass
        else:
            raise AssertionError("a missing recording did not fail")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    print("\nreplay layer: all checks passed")
