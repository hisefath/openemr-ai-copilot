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
    partial = "partial"    # W2: the deadline expired mid-graph; what was grounded is shown, with the gap named


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
    kind: Literal["patient", "schedule"] = "patient"  # schedule: user-bound session for the UC5 scan (evals, load tests)


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


# ---------------------------------------------------------------- Week 2: documents, evidence, staging
# The Week 2 flow adds two things Week 1 never had: facts that came off a page rather than out of a FHIR record,
# and guideline text that is not about this patient at all. Both need to stay distinguishable from chart data all
# the way to the screen, which is why source_type is on every citation rather than inferred later.


class SourceType(str, Enum):
    """Where a claim came from. The PRD requires the answer to separate patient-record facts from guideline
    evidence, so this is carried explicitly and rendered under separate headings — never merged."""
    chart = "chart"          # a FHIR record the server already held (Week 1 behaviour)
    document = "document"    # a field extracted from an uploaded lab PDF or intake form
    guideline = "guideline"  # a retrieved chunk of guideline text; about the condition, not the patient


class BBox(BaseModel):
    """A box on a rendered page, in PDF points, origin top-left.

    Produced by locate.py from the page's own word coordinates — never by the model. A vision model is unreliable
    at precise coordinates, and a drifting box is a citation that points at the wrong thing while looking
    authoritative. Worse, it would make the model the source of its own proof."""
    page: int = Field(description="1-based page number")
    x0: float
    y0: float
    x1: float
    y1: float


class Citation(BaseModel):
    """The PRD's required citation shape, plus the box.

    `bbox` is None when the value was extracted but could not be located on the page. The panel then renders
    "extracted, could not be located on the page" rather than pointing somewhere approximate. That is the
    mechanism that makes an unsupported extraction visible instead of plausible."""
    source_type: SourceType
    source_id: str = Field(description="OpenEMR document id, FHIR ResourceType/id, or guideline chunk id")
    page_or_section: str = Field(description="Page number for a document, heading or section id for a guideline")
    field_or_chunk_id: str = Field(description="Which field on the form, or which chunk of the corpus")
    quote_or_value: str = Field(description="The value as it appears in the source, verbatim")
    bbox: Optional[BBox] = Field(None, description="None means the value could not be located on the page")


class DocumentType(str, Enum):
    lab_pdf = "lab_pdf"
    intake_form = "intake_form"


class DocumentRef(BaseModel):
    """A document that round-tripped through OpenEMR. `content_hash` is OpenEMR's own — the standard API returns
    it on read — so re-ingesting the same file is detectable before anything is uploaded twice."""
    document_id: str = Field(description="OpenEMR document id; the citation anchor for every fact extracted from it")
    doc_type: DocumentType
    content_hash: str
    page_count: int
    uploaded_at: Optional[str] = None


class AbnormalFlag(str, Enum):
    """`unknown` is a fact about the document, not about the patient: the report did not say."""
    normal = "normal"
    low = "low"
    high = "high"
    critical_low = "critical_low"
    critical_high = "critical_high"
    abnormal = "abnormal"
    unknown = "unknown"


class LabResult(BaseModel):
    """One row of a lab report. Every field the PRD names, each traceable to the page it came from."""
    test_name: str
    value: str = Field(description="A string, not a number: '<0.01', 'negative' and 'trace' are real lab values "
                                   "and coercing them to a float either fails or silently invents precision")
    unit: Optional[str] = None
    reference_range: Optional[str] = Field(None, description="As printed, e.g. '3.5-5.1'; not parsed into bounds here")
    collection_date: Optional[str] = Field(None, description="ISO 8601 when the report gives one")
    abnormal_flag: AbnormalFlag = AbnormalFlag.unknown
    citation: Citation


class CitedValue(BaseModel):
    """A single extracted value and where on the page it came from."""
    value: str
    citation: Citation


class IntakeDemographics(BaseModel):
    """All optional: a front-desk form is routinely half-filled, and a missing field must stay missing rather
    than being inferred from the chart."""
    name: Optional[CitedValue] = None
    date_of_birth: Optional[CitedValue] = None
    sex: Optional[CitedValue] = None
    phone: Optional[CitedValue] = None
    address: Optional[CitedValue] = None


class IntakeMedication(BaseModel):
    name: str
    dose: Optional[str] = None
    frequency: Optional[str] = None
    citation: Citation


class IntakeAllergy(BaseModel):
    substance: str
    reaction: Optional[str] = Field(None, description="Empty means none written on the form, not none experienced")
    citation: Citation


class FamilyHistoryItem(BaseModel):
    condition: str
    relative: Optional[str] = None
    citation: Citation


class LabReport(BaseModel):
    """What the vision call is constrained to return for a lab PDF — the contract, not a shape checked afterwards."""
    doc_type: Literal[DocumentType.lab_pdf] = DocumentType.lab_pdf
    document_id: str
    results: List[LabResult] = Field(default_factory=list)
    unreadable_regions: List[str] = Field(default_factory=list,
                                          description="Parts of the scan the model could not read. Named, not dropped")


class IntakeForm(BaseModel):
    """What the vision call is constrained to return for an intake form."""
    doc_type: Literal[DocumentType.intake_form] = DocumentType.intake_form
    document_id: str
    demographics: IntakeDemographics = Field(default_factory=IntakeDemographics)
    chief_concern: Optional[CitedValue] = None
    medications: List[IntakeMedication] = Field(default_factory=list)
    allergies: List[IntakeAllergy] = Field(default_factory=list)
    family_history: List[FamilyHistoryItem] = Field(default_factory=list)
    unreadable_regions: List[str] = Field(default_factory=list)


ExtractedDocument = Annotated[Union[LabReport, IntakeForm], Field(discriminator="doc_type")]


class EvidenceChunk(BaseModel):
    """A guideline snippet that cleared the rerank floor. Never merged with chart facts (see SourceType)."""
    chunk_id: str
    text: str
    title: Optional[str] = None
    section: Optional[str] = None
    score: float = Field(description="Reranker score; only chunks above the floor reach the answer model")
    citation: Citation


# ---------------------------------------------------------------- Week 2: the graph (W2 spec §2)

class RouteTarget(str, Enum):
    extract = "extract"
    retrieve = "retrieve"
    answer = "answer"
    refuse = "refuse"    # short-circuits: without it an out-of-scope question runs the whole graph to reach a fixed string


class RoutingReason(str, Enum):
    """A closed enum, never free text.

    `llm.py`'s contract is that prompt and completion text are never logged or traced, and a free-text rationale
    logged on every handoff would regress that. Enum codes are also countable in Langfuse, which is strictly
    better observability than prose nobody aggregates."""
    no_document_extracted = "no_document_extracted"
    evidence_below_floor = "evidence_below_floor"
    extraction_exhausted = "extraction_exhausted"
    retrieval_exhausted = "retrieval_exhausted"
    deadline_expired = "deadline_expired"
    budget_exhausted = "budget_exhausted"
    ready_to_answer = "ready_to_answer"
    out_of_scope = "out_of_scope"


class RoutingDecision(BaseModel):
    """What the supervisor returns. It decides what happens next; the graph executes it."""
    next: RouteTarget
    reason: RoutingReason


class HandoffRecord(BaseModel):
    """Every transition, logged and traced AND returned in the API response — so the supervisor's routing is
    inspectable from outside without opening Langfuse. The PRD's named pitfall is an opaque supervisor."""
    from_node: str
    to_node: str
    reason: RoutingReason
    elapsed_ms: int
    correlation_id: str
    iteration: int = Field(0, description="Which loop of a ReAct worker this was")
    counterfactual: Optional[RouteTarget] = Field(
        None, description="What the deterministic policy would have chosen. Logged so the supervisor's value is "
                          "a measured rate in KEY_METRICS.md rather than an assumption")


# ---------------------------------------------------------------- Week 2: staged writes (W2 spec §6)

class StagedStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"   # kept, not deleted: a rejected extraction is a training signal and an eval case


class StagedFact(BaseModel):
    """A fact extracted from a document, waiting for a clinician.

    Keyed on (document_id, field_path) so re-ingesting the same document cannot create a second row — the PRD
    requires documents and derived observations to round-trip without duplicates."""
    document_id: str
    field_path: str = Field(description="Where in the extracted document this came from, e.g. 'allergies[0]'")
    fact_kind: Literal["allergy", "medication", "problem", "lab"]
    payload: Dict[str, str] = Field(description="The write body, as the OpenEMR standard API expects it")
    citation: Citation
    confidence: float = Field(ge=0.0, le=1.0)
    status: StagedStatus = StagedStatus.pending
    decided_by: Optional[str] = Field(None, description="fhirUser of the clinician who approved or rejected")
    decided_at: Optional[str] = None


# ---------------------------------------------------------------- Week 2: what the vision call returns
# Deliberately narrower than the stored shapes above. No bbox field is offered to the model AT ALL — not
# optional, not ignored: absent from the schema it is constrained to. Coordinates come from the page
# (locate.py). A field the model cannot fill is a field it cannot get wrong.
#
# What the model does supply is the page number and the label as printed beside the value. Those are what let
# the server find the value independently, which is the whole "vision extracts, code locates" division.


class SeenValue(BaseModel):
    value: str = Field(description="Exactly as printed, including units or symbols that are part of it")
    page: int = Field(description="1-based page this appears on")
    label_on_page: Optional[str] = Field(None, description="The field name printed beside it, verbatim")


class SeenLabResult(BaseModel):
    test_name: str = Field(description="As printed on the report")
    value: str = Field(description="As printed: keep '<0.01', 'negative', 'trace' exactly, never round")
    unit: Optional[str] = None
    reference_range: Optional[str] = Field(None, description="As printed, e.g. '3.5-5.1'")
    collection_date: Optional[str] = Field(None, description="ISO 8601 if the report states one")
    abnormal_flag: AbnormalFlag = Field(AbnormalFlag.unknown,
                                        description="Only if the report flags it. If it does not, use unknown")
    page: int
    label_on_page: Optional[str] = Field(None, description="Usually the test name as printed")


class SeenLabReport(BaseModel):
    results: List[SeenLabResult] = Field(default_factory=list)
    unreadable_regions: List[str] = Field(
        default_factory=list,
        description="Describe any part you could not read, rather than guessing or omitting it silently")


class SeenMedication(BaseModel):
    name: str
    dose: Optional[str] = None
    frequency: Optional[str] = None
    page: int
    label_on_page: Optional[str] = None


class SeenAllergy(BaseModel):
    substance: str
    reaction: Optional[str] = Field(None, description="Only if written. Blank means the form did not say")
    page: int
    label_on_page: Optional[str] = None


class SeenFamilyHistory(BaseModel):
    condition: str
    relative: Optional[str] = None
    page: int
    label_on_page: Optional[str] = None


class SeenDemographics(BaseModel):
    name: Optional[SeenValue] = None
    date_of_birth: Optional[SeenValue] = None
    sex: Optional[SeenValue] = None
    phone: Optional[SeenValue] = None
    address: Optional[SeenValue] = None


class SeenIntakeForm(BaseModel):
    demographics: SeenDemographics = Field(default_factory=SeenDemographics)
    chief_concern: Optional[SeenValue] = None
    medications: List[SeenMedication] = Field(default_factory=list)
    allergies: List[SeenAllergy] = Field(default_factory=list)
    family_history: List[SeenFamilyHistory] = Field(default_factory=list)
    unreadable_regions: List[str] = Field(default_factory=list)
