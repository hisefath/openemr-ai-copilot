"""In-memory sessions (ARCHITECTURE §1 browser -> agent boundary, §2 re-launch, AUDIT SEC-5).
The browser holds a 256-bit handle; the server keeps only sha256(handle). Single replica by design (§10, §11 moves this to Redis)."""
import hashlib
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Literal, NamedTuple, Optional, Set, Tuple

from schemas import RenderedLine

IDLE_TTL_S = 15 * 60
MAX_SESSIONS_PER_USER = 3
HISTORY_TURNS = 6
PREFETCH_REUSE_S = 120


def handle_hash(handle: str) -> str:
    return hashlib.sha256(handle.encode()).hexdigest()


class Turn(NamedTuple):
    """Question plus server-rendered verified lines only, never raw model output (§4.2 step 2)."""
    question: str
    lines: List[RenderedLine]


@dataclass(eq=False, repr=False)
class Session:
    session_ref: str                             # random; the only session id allowed in logs, Langfuse and audit rows
    kind: Literal["patient", "schedule"]
    source: Literal["launch", "api", "schedule"]
    fhir_user: str                               # e.g. Practitioner/<uuid>
    client_id: Optional[str]
    patient_id: Optional[str]                    # None for schedule sessions
    access_token: str
    token_expires_at: float                      # epoch seconds
    created_at: float
    last_used_at: float
    allowed_patient_ids: Set[str] = field(default_factory=set)  # schedule: today's appointment patients (§2)
    history: Deque[Turn] = field(default_factory=lambda: deque(maxlen=HISTORY_TURNS))
    context: Any = None
    context_fetched_at: Optional[float] = None
    prefetch_task: Any = None

    @property
    def expires_at(self) -> float:
        return min(self.last_used_at + IDLE_TTL_S, self.token_expires_at)

    def __repr__(self) -> str:
        """Ids only as §7 allows in logs: no handle, token, user, client or patient ids."""
        return f"Session(session_ref={self.session_ref!r}, kind={self.kind!r}, source={self.source!r})"


class SessionStore:
    # ponytail: O(sessions) purge on every call; fine for one replica, Redis TTL keys when scaling out (§11)
    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self._sessions: Dict[str, Session] = {}                      # sha256(handle) -> session
        self._prefetch: Dict[Tuple[str, str], Tuple[float, Any]] = {}

    def create(self, *, kind: Literal["patient", "schedule"], source: Literal["launch", "api", "schedule"], fhir_user: str,
               client_id: Optional[str], access_token: str, token_expires_at: float,
               patient_id: Optional[str] = None) -> Tuple[str, Session]:
        """New session; the user's least recently used sessions are evicted so at most 3 remain (§1)."""
        self.purge()
        now = self.clock()
        mine = sorted((h for h, s in self._sessions.items() if s.fhir_user == fhir_user),
                      key=lambda h: self._sessions[h].last_used_at)
        for h in mine[:max(0, len(mine) - MAX_SESSIONS_PER_USER + 1)]:
            del self._sessions[h]
        handle = secrets.token_urlsafe(32)
        session = Session(session_ref=secrets.token_hex(8), kind=kind, source=source, fhir_user=fhir_user,
                          client_id=client_id, patient_id=patient_id, access_token=access_token,
                          token_expires_at=token_expires_at, created_at=now, last_used_at=now)
        self._sessions[handle_hash(handle)] = session
        return handle, session

    def get(self, handle: str) -> Optional[Session]:
        """Live session for a bearer handle, refreshing its idle timer."""
        self.purge()
        session = self._sessions.get(handle_hash(handle))
        if session is not None:
            session.last_used_at = self.clock()
        return session

    def put_prefetch(self, fhir_user: str, patient_id: str, value: Any) -> None:
        """Remember prefetched data (or the in-flight task) so a re-launch within 120 s reuses it (§2 re-launch)."""
        self.purge()
        self._prefetch[(fhir_user, patient_id)] = (self.clock(), value)

    def get_prefetch(self, fhir_user: str, patient_id: str) -> Optional[Any]:
        self.purge()
        hit = self._prefetch.get((fhir_user, patient_id))
        return hit[1] if hit else None

    def purge(self) -> None:
        """Drop expired sessions (tokens, cached PHI) and prefetch entries past 120 s. Every call runs it; main.py may also
        call it on a timer so an idle process keeps no PHI in memory."""
        now = self.clock()
        self._sessions = {h: s for h, s in self._sessions.items() if self._live(s, now)}
        self._prefetch = {k: v for k, v in self._prefetch.items() if now - v[0] < PREFETCH_REUSE_S}

    @staticmethod
    def _live(session: Session, now: float) -> bool:
        return now - session.last_used_at < IDLE_TTL_S and now < session.token_expires_at
