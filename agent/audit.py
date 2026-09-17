"""Audit rows in copilot.copilot_audit through an INSERT-only MySQL user over verified TLS (ARCHITECTURE §7, AUDIT COMP-7).
A failed write raises AuditUnavailable and the caller returns no PHI (FM-15)."""
import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, List, Optional

import pymysql

from config import Settings
from observability import error_code
from schemas import AuditEvent

log = logging.getLogger("agent.audit")

COLUMNS = tuple(AuditEvent.model_fields)  # column names come from the contract, never from input
INSERT_SQL = f"INSERT INTO copilot_audit ({', '.join(COLUMNS)}) VALUES ({', '.join(['%s'] * len(COLUMNS))})"
WRITE_TIMEOUT_S = 3.0  # longest a caller waits for one row, queueing included
FAIL_FAST_S = 5.0      # after a database error, writes fail at once for this long instead of piling up on connects


class AuditUnavailable(Exception):
    """The audit row was not written. Callers fail closed (FM-15). Carries no database message."""


class AuditWriter:
    # ponytail: one connection on one dedicated thread; use a small pool if audit writes queue up under load.
    def __init__(self, settings: Settings, clock: Callable[[], float] = time.monotonic):
        self._settings = settings
        self._clock = clock
        self._conn: Optional[pymysql.connections.Connection] = None
        self._down_until = 0.0
        # Own thread, so a hung database never fills the default executor that asyncio uses for DNS lookups.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="audit")

    async def write(self, event: AuditEvent, timeout: float = WRITE_TIMEOUT_S) -> None:
        """Waits at most `timeout` s (pass the request's remaining deadline when shorter). A row still queued at the
        timeout is never written; one already being inserted may still land after the caller has failed closed."""
        try:
            fut = asyncio.get_running_loop().run_in_executor(self._executor, self._insert, event)
            await asyncio.wait_for(fut, max(0.0, min(timeout, WRITE_TIMEOUT_S)))
        except Exception as e:
            log.error("audit write failed", extra={"event": event.event.value, "error": error_code(e)})
            raise AuditUnavailable(type(e).__name__) from None  # the DB message can echo row values

    def _insert(self, event: AuditEvent) -> None:
        if self._clock() < self._down_until:
            raise ConnectionRefusedError("audit database failing fast")
        row = tuple(event.model_dump(mode="json").values())
        try:
            if self._conn is None:
                self._conn = self._connect()
            self._conn.ping(reconnect=True)  # MySQL drops idle connections; don't fail a request over that
            with self._conn.cursor() as cur:
                cur.execute(INSERT_SQL, row)
        except Exception:
            self._down_until = self._clock() + FAIL_FAST_S
            self._close()
            raise

    def _connect(self) -> pymysql.connections.Connection:
        s = self._settings
        if not (s.audit_db_host and s.audit_db_user and s.audit_db_ca):
            raise RuntimeError("audit database not configured")  # no CA, no connection: never plaintext
        return pymysql.connect(host=s.audit_db_host, port=s.audit_db_port, user=s.audit_db_user,
                               password=s.audit_db_password or "", database=s.audit_db_name,
                               ssl={"ca": s.audit_db_ca, "check_hostname": True}, autocommit=True,
                               connect_timeout=2, read_timeout=2, write_timeout=2)

    def _close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


class FakeAuditWriter:
    """In-memory writer for tests and local runs without the audit database."""

    def __init__(self, fail: bool = False):
        self.events: List[AuditEvent] = []
        self.fail = fail

    async def write(self, event: AuditEvent, timeout: float = WRITE_TIMEOUT_S) -> None:
        if self.fail:
            raise AuditUnavailable("FakeAuditWriter")
        self.events.append(event)
