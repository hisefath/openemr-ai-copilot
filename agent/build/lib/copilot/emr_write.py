"""Writes to OpenEMR's standard REST API.

Week 1 never wrote anything: `fhir.py`'s client exposes only `get()`. This is the whole write surface, and it is
the standard API rather than FHIR because FHIR is read-only for documents in OpenEMR 8.5 — no DocumentReference
write, no Observation write. Everything here was verified against a running instance before it was written down.

THREE THINGS THAT ARE NOT OBVIOUS, each of which cost a debugging session to find:

1. THE CATEGORY PATH MUST USE UNDERSCORES, AND HAS A THROWAWAY FIRST SEGMENT.
   `?path=Categories/Lab_Report`, never `?path=Lab Report`. `DocumentService::isValidPath` drops the first
   segment, so a single-segment path validates having checked nothing, and `getLastIdOfPath` matches against
   `replace(LOWER(name),' ','')`, so a segment with a literal space never resolves. The failure is silent: the
   upload returns `200 true`, the file lands on disk, the row lands in `documents` — and `list_id` stays 0, no
   category row is written, and the document is unreachable through the API forever.

2. THE UPLOAD RETURNS A BARE `true`, NEVER AN ID.
   `DocumentService::insertAtPath` returns bool. The citation anchor therefore comes from a follow-up list call,
   not from the write.

3. IDENTIFIERS DISAGREE BY ROUTE.
   `document` takes the **numeric pid**; `allergy`, `medication` and `medical_problem` take the **puuid**. The
   agent's session holds a uuid, so `resolve_pid` translates — which needs `user/patient.crus` on top of the
   write scopes.

Idempotency is keyed on a content hash carried in the FILENAME. OpenEMR's own `documents.hash` column was
measured and is not the sha512 of the content nor of the stored bytes, so it cannot be compared against a local
digest. Putting our own digest in the filename means a re-ingest is detectable from the list response alone, with
no extra state to keep in sync and nothing to migrate.

Scopes required (exact strings from src/RestControllers/OpenApi/OpenApiDefinitions.php — OpenEMR uses a `cruds`
suffix, c=create r=read u=update d=delete s=search, and has no `.write`):
    api:oemr  user/document.crs  user/allergy.cruds  user/medical_problem.cruds  user/medication.cruds
    user/patient.crus
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from typing import Any, Dict, List, NamedTuple, Optional

import httpx

from .deadline import Deadline

log = logging.getLogger("agent")

UPLOAD_FIELD = "document"          # the multipart field name the route reads from $_FILES
HASH_CHARS = 16                    # enough that a collision is not a practical concern in one patient's chart
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class WriteResult(NamedTuple):
    """Writes report failure explicitly. A write that quietly did nothing is the worst outcome available here:
    the review queue would show `approved` for a record the chart never received."""
    ok: bool
    status: Optional[int]
    data: Optional[Dict[str, Any]] = None
    detail: Optional[str] = None


def category_path(category: str) -> str:
    """Build the one path shape OpenEMR actually resolves.

    "Lab Report" -> "Categories/Lab_Report". Spaces become underscores because the lookup compares against
    `replace(LOWER(name),' ','')`; the leading "Categories" is the segment isValidPath discards."""
    return "Categories/" + _SAFE_NAME.sub("_", category.strip())


def content_filename(prefix: str, data: bytes, ext: str = "pdf") -> str:
    """`lab_5c06112e6d235e66.pdf` — the digest is the idempotency key, visible in OpenEMR's own UI."""
    digest = hashlib.sha256(data).hexdigest()[:HASH_CHARS]
    return f"{_SAFE_NAME.sub('_', prefix)}_{digest}.{ext}"


class EmrWriteClient:
    """One per process, mirroring FhirClient: same concurrency discipline, same deadline threading."""

    def __init__(self, http: httpx.AsyncClient, fhir_base: str, concurrency: int = 6):
        # The standard API sits beside FHIR under the same /apis/{site}/ root.
        self._base = re.sub(r"/fhir/?$", "/api", fhir_base.rstrip("/"))
        self._http = http
        self._sem = asyncio.Semaphore(concurrency)

    async def _request(self, method: str, path: str, *, token: str, deadline: Deadline, correlation_id: str,
                       **kw: Any) -> WriteResult:
        """Never raises for transport or HTTP failures; they come back as ok=False with a reason."""
        if deadline.expired():
            return WriteResult(False, None, detail="deadline")
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json",
                   "X-Correlation-ID": correlation_id}
        try:
            async with asyncio.timeout(deadline.remaining()):
                async with self._sem:
                    r = deadline.remaining()
                    resp = await self._http.request(
                        method, f"{self._base}/{path.lstrip('/')}", headers=headers,
                        timeout=httpx.Timeout(connect=min(2.0, r), read=min(15.0, r),
                                              write=min(15.0, r), pool=min(2.0, r)), **kw)
        except (asyncio.TimeoutError, TimeoutError):
            return WriteResult(False, None, detail="timeout")
        except httpx.HTTPError as e:
            return WriteResult(False, None, detail=type(e).__name__)

        if resp.status_code >= 400:
            # Never echo the body: OpenEMR error payloads can carry request content back (COMP-3).
            return WriteResult(False, resp.status_code, detail=f"http_{resp.status_code}")
        try:
            body = resp.json()
        except ValueError:
            return WriteResult(False, resp.status_code, detail="unparseable")
        return WriteResult(True, resp.status_code, body if isinstance(body, dict) else {"data": body})

    # ------------------------------------------------------------------ identifiers

    async def resolve_pid(self, puuid: str, **ctx: Any) -> Optional[int]:
        """The numeric pid for a patient uuid. Document routes take pid; every other write route takes puuid."""
        res = await self._request("GET", f"patient/{puuid}", **ctx)
        if not res.ok:
            return None
        row = (res.data or {}).get("data") or {}
        pid = row.get("pid")
        try:
            return int(pid)
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------ documents

    async def list_documents(self, pid: int, path: str, **ctx: Any) -> List[Dict[str, Any]]:
        """The list call is also how a document id is obtained at all — the upload does not return one."""
        res = await self._request("GET", f"patient/{pid}/document", params={"path": path}, **ctx)
        if not res.ok:
            return []
        data = (res.data or {}).get("data")
        return data if isinstance(data, list) else []

    async def find_document(self, pid: int, path: str, filename: str, **ctx: Any) -> Optional[Dict[str, Any]]:
        return next((d for d in await self.list_documents(pid, path, **ctx) if d.get("filename") == filename), None)

    async def upload_document(self, pid: int, category: str, data: bytes, *, prefix: str = "doc",
                              ext: str = "pdf", content_type: str = "application/pdf",
                              **ctx: Any) -> WriteResult:
        """Upload once. Re-running with identical bytes returns the existing record instead of duplicating it —
        the PRD requires documents to round-trip 'without creating duplicate or untraceable records'.

        Returns the document row (id, filename, hash, mimetype) so the caller has its citation anchor."""
        path, name = category_path(category), content_filename(prefix, data, ext)

        existing = await self.find_document(pid, path, name, **ctx)
        if existing:
            return WriteResult(True, 200, {"data": existing, "deduplicated": True})

        res = await self._request("POST", f"patient/{pid}/document", params={"path": path},
                                  files={UPLOAD_FIELD: (name, data, content_type)}, **ctx)
        if not res.ok:
            return res

        row = await self.find_document(pid, path, name, **ctx)
        if row is None:
            # 200 with nothing listed back is the silent-orphan signature (see the module docstring). Report it
            # as a failure: a document we cannot cite is not a document we stored.
            return WriteResult(False, res.status, detail="uploaded_but_not_listed")
        return WriteResult(True, res.status, {"data": row, "deduplicated": False})

    # ------------------------------------------------------------------ structured records

    async def write_record(self, puuid: str, kind: str, payload: Dict[str, Any], **ctx: Any) -> WriteResult:
        """POST one approved fact. `kind` is allergy | medication | medical_problem.

        Provenance rides in `comments`, which is whitelisted for insert on these controllers — and reads back out
        through this same standard API. It is NOT projected by the FHIR services, so a provenance read-back has
        to use this route, not `GET /fhir/AllergyIntolerance`."""
        if kind not in ("allergy", "medication", "medical_problem"):
            return WriteResult(False, None, detail=f"unsupported_kind:{kind}")
        return await self._request("POST", f"patient/{puuid}/{kind}", json=payload, **ctx)

    async def list_records(self, puuid: str, kind: str, **ctx: Any) -> List[Dict[str, Any]]:
        res = await self._request("GET", f"patient/{puuid}/{kind}", **ctx)
        data = (res.data or {}).get("data") if res.ok else None
        return data if isinstance(data, list) else []
