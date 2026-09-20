"""A per-request time budget passed through FHIR calls, tools and the Claude call (ARCHITECTURE §4.2)."""
import time


class Deadline:
    def __init__(self, seconds: float, clock=time.monotonic):
        self._clock = clock
        self._end = clock() + seconds

    def remaining(self) -> float:
        return max(0.0, self._end - self._clock())

    def expired(self) -> bool:
        return self.remaining() <= 0.0

    def has(self, seconds: float) -> bool:
        """True if at least `seconds` remain (used to decide whether a retry or tool round fits)."""
        return self.remaining() >= seconds


if __name__ == "__main__":
    t = [100.0]
    d = Deadline(9.0, clock=lambda: t[0])
    assert d.has(9.0) and not d.expired()
    t[0] += 6.5
    assert d.has(2.5) and not d.has(3.0)
    t[0] += 10
    assert d.expired() and d.remaining() == 0.0
    print("deadline ok")
