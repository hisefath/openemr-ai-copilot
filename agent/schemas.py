"""Contracts: every record the agent reasons over, every tool input/output, the answer plan Claude returns,
the rendered answer the physician sees, the HTTP API and audit events.
These models are the source of truth; JSON schemas for Claude and the API are generated from them.
See ARCHITECTURE.md sections 3-8."""
from enum import Enum
from typing import Dict, Generic, List, Literal, Optional, TypeVar, Union

from pydantic import BaseModel, Field
from typing_extensions import Annotated


class LoadStatus(str, Enum):
    """What happened when a resource type was fetched. Only `ok` and `empty` are facts about the chart."""
    pending = "pending"      # fetch still running
    ok = "ok"                # records returned
    empty = "empty"          # 200 with no records -> "none recorded", never "none"
    forbidden = "forbidden"  # 403: the user's role can't view this resource
    expired = "expired"      # 401: token expired or revoked -> relaunch
    error = "error"          # other 4xx/5xx, unparseable body
    timeout = "timeout"      # no answer within the budget


class Record(BaseModel):
    source_ids: List[str] = Field(description="ResourceType/id of every FHIR record this was built from")
    date: Optional[str] = Field(None, description="Most relevant date, ISO 8601 (YYYY-MM-DD when only a date is known)")


class PatientRecord(Record):
    """Name, DOB and MRN are for the on-screen banner only; the model receives age and sex (ARCHITECTURE §1)."""
    name: str
    birth_date: Optional[str] = None
    gender: Optional[str] = None
    mrn: Optional[str] = None


class AllergyRecord(Record):
    substance: str
    coded: bool = Field(description="False when OpenEMR had no code and the name came from narrative text (AUDIT DQ-2)")
    clinical_status: Optional[str] = None
    verification_status: Optional[str] = None
    criticality: Optional[str] = None
    severity: Optional[str] = Field(None, description="mild/moderate/severe; OpenEMR stores it in the reaction manifestation")
    reactions: List[str] = Field(default_factory=list, description="Empty means no reaction recorded, not no reaction")


class MedicationRecord(Record):
    name: Optional[str] = Field(description="None when OpenEMR returned a row with no medication (AUDIT DQ-M3)")
    rxnorm: Optional[str] = None
    status: Optional[str] = None
    dosage: Optional[str] = None
    status_conflict: bool = Field(False, description="The same drug has rows with different statuses")
    statuses: List[str] = Field(default_factory=list)
    possibly_stale: bool = Field(False, description="Marked active but authored more than 5 years ago")


class ConditionRecord(Record):
    name: str
    clinical_status: Optional[str] = None
    kind: Optional[str] = Field(None, description="SNOMED semantic tag from the name, e.g. disorder, finding, situation")
    onset: Optional[str] = None
    abatement: Optional[str] = None


class LabRecord(Record):
    name: str
    loinc: Optional[str] = None
    value: Optional[float] = Field(None, description="None when no value was recorded (placeholder or data-absent)")
    unit: Optional[str] = None
    status: Optional[str] = None
    abnormal_flag: Optional[str] = Field(None, description="As recorded; None means not flagged, never 'normal'")


class VitalRecord(Record):
    name: str
    loinc: Optional[str] = None
    value: float
    unit: Optional[str] = None
    unit_suspect: bool = Field(False, description="Value is implausible for its unit (e.g. height 163 [in_i])")


class EncounterRecord(Record):
    type: Optional[str] = None
    reason: Optional[str] = None


R = TypeVar("R", bound=Record)


class ResourceLoad(BaseModel, Generic[R]):
    status: LoadStatus
    records: List[R] = Field(default_factory=list)
    detail: Optional[str] = Field(None, description="Non-PHI reason for a failure, e.g. 'HTTP 401'")


class PatientContext(BaseModel):
    """Everything fetched for one patient in one session, normalized and cited."""
    patient_id: str
    patient: ResourceLoad[PatientRecord]
    allergies: ResourceLoad[AllergyRecord]
    medications: ResourceLoad[MedicationRecord]
    conditions: ResourceLoad[ConditionRecord]
    labs: ResourceLoad[LabRecord]
    vitals: ResourceLoad[VitalRecord]
    encounters: ResourceLoad[EncounterRecord]
    fetched_at: str


# ---------------------------------------------------------------- rules

class Flag(BaseModel):
    """A deterministic rule result. Messages are fixed templates written by rules.py, never by the model."""
    rule_id: str
    severity: Literal["high", "medium"]
    message: str
    source_ids: List[str]


# ---------------------------------------------------------------- what Claude returns (ARCHITECTURE §4.1)

class Section(str, Enum):
    visit_context = "visit_context"
    safety = "safety"
    recent_results = "recent_results"
    changes = "changes"
    background = "background"


class Intent(str, Enum):
    brief = "brief"
    safety_check = "safety_check"
    changes = "changes"
    follow_up = "follow_up"
    other = "other"


class ScopeViolation(str, Enum):
    none = "none"
    other_patient = "other_patient"              # asks about a patient other than the session's
    bulk_request = "bulk_request"                # asks for many/all patients
    instruction_in_data = "instruction_in_data"  # chart text tries to instruct the assistant


class RecordItem(BaseModel):
    kind: Literal["record"] = "record"
    source_id: str = Field(description="A source id exactly as given in the context, e.g. MedicationRequest/abc")
    section: Section


class TrendItem(BaseModel):
    kind: Literal["trend"] = "trend"
    lab: str = Field(description="LOINC code (preferred) or lab name exactly as given in the context")
    section: Section = Section.changes


PlanItem = Annotated[Union[RecordItem, TrendItem], Field(discriminator="kind")]


class Clarify(BaseModel):
    candidate_source_ids: List[str] = Field(min_length=2, max_length=4,
                                            description="The records the question could refer to")


class AnswerPlan(BaseModel):
    """Claude selects and orders records; it never writes clinical text."""
    intent: Intent
    scope_violation: ScopeViolation = ScopeViolation.none
    items: List[PlanItem] = Field(default_factory=list, max_length=15, description="Most relevant first")
    proposed_drugs: List[str] = Field(default_factory=list, max_length=5,
                                      description="Drug names the physician asked about starting or giving")
    clarify: Optional[Clarify] = None


# ---------------------------------------------------------------- tools (ARCHITECTURE §3)

class LabHistoryInput(BaseModel):
    lab: str = Field(min_length=1, max_length=80, description="LOINC code or lab name")
    since: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$", description="YYYY-MM-DD")


class EncountersInput(BaseModel):
    since: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$", description="YYYY-MM-DD")


class ToolName(str, Enum):
    get_lab_history = "get_lab_history"
    get_encounters = "get_encounters"
    scan_todays_schedule = "scan_todays_schedule"


# ---------------------------------------------------------------- what the physician sees (ARCHITECTURE §5, §8)

class Outcome(str, Enum):
    passed = "pass"
    pass_with_removals = "pass_with_removals"
    fail = "fail"          # fallback shown
    refused = "refused"    # scope violation
    clarify = "clarify"


class RenderedLine(BaseModel):
    text: str = Field(description="Written by server templates from normalized records")
    source_ids: List[str]
    date: Optional[str] = None
    older_than_12_months: bool = False


class RenderedSection(BaseModel):
    section: Section
    lines: List[RenderedLine]


class Coverage(BaseModel):
    resource: str
    status: LoadStatus
    text: str = Field(description="Server-written, e.g. 'No allergies recorded in OpenEMR (as of 10:42)'")


class PatientBanner(BaseModel):
    name: str
    birth_date: Optional[str] = None
    mrn: Optional[str] = None
    sex: Optional[str] = None
    age: Optional[int] = None


class MessageRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    selected_source_id: Optional[str] = Field(None, max_length=120,
                                              description="Set when the physician picks a clarify chip")
    client_request_id: Optional[str] = Field(None, pattern=r"^[0-9a-fA-F-]{36}$")


class MessageResponse(BaseModel):
    correlation_id: str
    outcome: Outcome
    patient_banner: PatientBanner
    sections: List[RenderedSection] = Field(default_factory=list)
    flags: List[Flag] = Field(default_factory=list)
    coverage: List[Coverage] = Field(default_factory=list)
    clarify: List[RenderedLine] = Field(default_factory=list)
    withheld_count: int = 0
    notice: Optional[str] = Field(None, description="Fixed server text: refusal, fallback reason, unchecked drugs")
    data_as_of: str


class SessionCreateRequest(BaseModel):
    access_token: str = Field(min_length=10, max_length=8192)
    patient_id: Optional[str] = Field(None, pattern=r"^[0-9a-fA-F-]{36}$")


class SessionCreateResponse(BaseModel):
    session_handle: str
    patient_banner: Optional[PatientBanner] = None
    expires_at: str


class SessionStatus(BaseModel):
    kind: Literal["patient", "schedule"]
    patient_banner: Optional[PatientBanner] = None
    data_as_of: Optional[str] = None
    load_statuses: Dict[str, LoadStatus] = Field(default_factory=dict)
    flags: List[Flag] = Field(default_factory=list)


class ScanCounts(BaseModel):
    scheduled: int
    checked: int
    failed: int
    flagged: int


class ScanPatient(BaseModel):
    patient_banner: PatientBanner
    appointment_start: Optional[str] = None
    status: Literal["checked", "failed"]
    failed_resources: List[str] = Field(default_factory=list)
    flags: List[Flag] = Field(default_factory=list)


class ScheduleScanResponse(BaseModel):
    correlation_id: str
    counts: ScanCounts
    patients: List[ScanPatient] = Field(description="Flagged or failed patients only")
    notice: str = "Recurring appointments are not visible through OpenEMR FHIR and were not checked."


class ErrorBody(BaseModel):
    code: str
    message: str
    correlation_id: str


class ErrorResponse(BaseModel):
    error: ErrorBody


# ---------------------------------------------------------------- audit (ARCHITECTURE §7)

class AuditEventType(str, Enum):
    launch = "launch"
    session_create = "session_create"
    question = "question"
    fhir_read = "fhir_read"
    llm_call = "llm_call"
    refusal = "refusal"
    denied = "denied"


class AuditEvent(BaseModel):
    """One row in copilot_audit. No clinical values, no question text."""
    event: AuditEventType
    ts_ms: int
    correlation_id: str
    session_ref: Optional[str] = None
    fhir_user: Optional[str] = None
    client_id: Optional[str] = None
    source: Optional[Literal["launch", "api", "schedule"]] = None
    patient_id: Optional[str] = None
    intent: Optional[str] = None
    fhir_path: Optional[str] = Field(None, description="Path without query string, e.g. /fhir/MedicationRequest")
    http_status: Optional[int] = None
    record_count: Optional[int] = None
    outcome: Optional[str] = None
    detail: Optional[str] = Field(None, max_length=200, description="Non-PHI reason code")
