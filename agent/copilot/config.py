"""All configuration, read once from the environment. Secrets never have defaults."""
import os
from typing import FrozenSet, Optional

from pydantic import BaseModel


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


class Settings(BaseModel):
    # OpenEMR
    public_issuer: str            # FHIR base as OpenEMR advertises it (site_addr_oath); checked against launch iss/aud
    fhir_base: str                # FHIR base the agent calls (may be a private network address)
    oauth_public_base: str        # browser-facing OAuth base, e.g. https://emr.example/oauth2/default
    oauth_internal_base: str      # server-to-server OAuth base (token, introspect)
    openemr_public_origin: str    # for CSP frame-ancestors
    client_id: Optional[str]
    client_secret: Optional[str]
    openemr_concurrency: int = 6

    # Agent
    agent_public_url: str
    allow_api_sessions: bool = False
    eval_patient_ids: FrozenSet[str] = frozenset()
    hmac_key: Optional[str]       # pseudonymizes ids sent to Langfuse; required in production

    # LLM
    anthropic_model: str = "claude-haiku-4-5"
    question_deadline_s: float = 9.0

    # Audit (copilot_audit table in the OpenEMR MySQL, INSERT-only user, verified TLS)
    audit_db_host: Optional[str]
    audit_db_port: int = 3306
    audit_db_user: Optional[str]
    audit_db_password: Optional[str]
    audit_db_name: str = "copilot"
    audit_db_ca: Optional[str]    # PEM path of the CA that signed the MySQL server cert

    @classmethod
    def from_env(cls) -> "Settings":
        fhir = (_env("OPENEMR_FHIR_BASE") or "").rstrip("/")
        public_issuer = (_env("PUBLIC_ISSUER", fhir) or "").rstrip("/")
        origin = _env("OPENEMR_PUBLIC_ORIGIN", public_issuer.split("/apis/")[0])
        return cls(
            public_issuer=public_issuer,
            fhir_base=fhir,
            oauth_public_base=(_env("OAUTH_PUBLIC_BASE", f"{origin}/oauth2/default") or "").rstrip("/"),
            oauth_internal_base=(_env("OAUTH_INTERNAL_BASE", fhir.split("/apis/")[0] + "/oauth2/default") or "").rstrip("/"),
            openemr_public_origin=origin,
            client_id=_env("SMART_CLIENT_ID"),
            client_secret=_env("SMART_CLIENT_SECRET"),
            openemr_concurrency=int(_env("OPENEMR_CONCURRENCY", "6")),
            agent_public_url=(_env("AGENT_PUBLIC_URL", "http://localhost:8000") or "").rstrip("/"),
            allow_api_sessions=_env("ALLOW_API_SESSIONS", "false").lower() == "true",
            eval_patient_ids=frozenset(filter(None, (_env("EVAL_PATIENT_IDS", "") or "").split(","))),
            hmac_key=_env("HMAC_KEY"),
            anthropic_model=_env("ANTHROPIC_MODEL", "claude-haiku-4-5"),
            question_deadline_s=float(_env("QUESTION_DEADLINE_S", "9")),
            audit_db_host=_env("AUDIT_DB_HOST"),
            audit_db_port=int(_env("AUDIT_DB_PORT", "3306")),
            audit_db_user=_env("AUDIT_DB_USER"),
            audit_db_password=_env("AUDIT_DB_PASSWORD"),
            audit_db_name=_env("AUDIT_DB_NAME", "copilot"),
            audit_db_ca=_env("AUDIT_DB_CA"),
        )
