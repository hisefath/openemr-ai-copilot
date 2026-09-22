"""Record once, score anytime — at the seam Week 1 already has.

`main.app.state.llm` is the only place the agent talks to Anthropic, and `agent/tests/` already swaps it for a
fake. This module puts two real clients at that same seam:

  RecordingClient  wraps the live client, captures every response to disk. Run deliberately, rarely.
  ReplayClient     serves those captures back. No network, no key, no sampling variance.

Why replay at all: at n=10 a 90% pass rate carries +-19 points, so a 5% regression threshold measured against live
runs is inside the noise band. Replay removes the variance, which is what makes the threshold mean something.

THE KEYING RULE — this is the part that makes the gate able to fail.

Replay pins the model's *response*. Everything downstream of it (parsing, verification, rendering, the rules
engine, the scorer) still executes live, so a regression there is caught natively. The blind spot is anything
that only reaches behaviour *through* the model: the prompt text, the model id, the tool definitions, the output
schema. If recordings were keyed on the case id alone, editing the system prompt would replay the old response
unchanged and the build would stay green -- which is exactly the regression a grader introduces.

So recordings are keyed on a hash of that model-facing surface, and:

    A CACHE MISS IS A HARD FAILURE. Never a live call, never a silent pass.

Deliberately NOT in the key:
  messages  -- case content, keyed separately by case id + call index. Hashing it would mean any fixture tweak
               invalidated every recording, which is unbearable mid-sprint.
  timeout   -- `_call` sets it from `deadline.remaining()`, a different float on every run. Hashing it would make
               every single replay a miss.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional

from anthropic.types import Message

RECORDINGS = Path(__file__).parent / "recordings"


class CacheMiss(RuntimeError):
    """No recording matches this call. Always fatal to the case — see the module docstring."""


def _system_text(system: Any) -> str:
    """The prompt text only. cache_control is a caching hint and does not change what the model returns."""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return "\n".join(b.get("text", "") for b in system if isinstance(b, dict))
    return ""


def surface_key(kwargs: dict) -> str:
    """Hash the model-facing surface: change any of this and the recorded response is no longer trustworthy."""
    surface = {
        "model": kwargs.get("model"),
        "max_tokens": kwargs.get("max_tokens"),
        "system": _system_text(kwargs.get("system")),
        "tools": kwargs.get("tools") or [],
        "tool_choice": kwargs.get("tool_choice"),
        "output_config": kwargs.get("output_config"),
    }
    blob = json.dumps(surface, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _path(case_id: str, root: Optional[Path] = None) -> Path:
    return (root or RECORDINGS) / f"{case_id}.json"


class _Messages:
    def __init__(self, owner: "ReplayClient" | "RecordingClient") -> None:
        self._owner = owner

    async def create(self, **kwargs: Any) -> Message:
        return await self._owner._create(**kwargs)


class ReplayClient:
    """Serves recorded responses. Raises CacheMiss rather than guessing, calling out, or passing quietly."""

    max_retries = 0  # llm.py reads this for the Langfuse span

    def __init__(self, case_id: str, root: Optional[Path] = None) -> None:
        self.case_id, self._root, self._i = case_id, root, 0
        # The app catches exceptions and returns a 500, so the reason has to survive out-of-band or the
        # gate reports an unactionable "HTTP 500" instead of naming what changed.
        self.miss: Optional[str] = None
        self.messages = _Messages(self)
        path = _path(case_id, root)
        if not path.exists():
            raise CacheMiss(f"{case_id}: no recording file at {path} — record it with --record")
        self._calls = json.loads(path.read_text())["calls"]

    def with_options(self, **_: Any) -> "ReplayClient":
        return self

    async def _create(self, **kwargs: Any) -> Message:
        i, want = self._i, surface_key(kwargs)
        self._i += 1
        if i >= len(self._calls):
            raise self._miss(
                f"{self.case_id}: the agent made call #{i + 1} but only {len(self._calls)} were recorded. "
                f"The agent's control flow changed. Re-record deliberately."
            )
        got = self._calls[i]["surface"]
        if got != want:
            raise self._miss(
                f"{self.case_id}: model-facing surface changed on call #{i + 1} "
                f"(recorded {got}, now {want}). The prompt, model, tools or output schema were edited, so the "
                f"recorded response no longer tells us what the model would do. Re-record deliberately."
            )
        return Message.model_validate(self._calls[i]["response"])

    def _miss(self, message: str) -> CacheMiss:
        self.miss = message
        return CacheMiss(message)


class RecordingClient:
    """Wraps the live client and writes what it says to disk. Deliberate, and rare."""

    def __init__(self, live: Any, case_id: str, root: Optional[Path] = None) -> None:
        self._live, self.case_id, self._root, self._calls = live, case_id, root, []
        self.messages = _Messages(self)

    @property
    def max_retries(self) -> Any:
        return getattr(self._live, "max_retries", None)

    def with_options(self, **kw: Any) -> "RecordingClient":
        self._live = self._live.with_options(**kw)
        return self

    async def _create(self, **kwargs: Any) -> Message:
        resp = await self._live.messages.create(**kwargs)
        self._calls.append({"surface": surface_key(kwargs), "response": json.loads(resp.model_dump_json())})
        return resp

    def save(self) -> Path:
        path = _path(self.case_id, self._root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"case_id": self.case_id, "calls": self._calls}, indent=2) + "\n")
        return path
