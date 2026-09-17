"""Contracts: every record the agent reasons over, every tool input/output, and the answer shape.
These models are the source of truth; JSON schemas for Claude and the API are generated from them."""
from enum import Enum
from typing import Generic, List, Optional, TypeVar

from pydantic import BaseModel, Field


class LoadStatus(str, Enum):
    """What happened when a resource type was fetched. Only `ok` and `empty` are facts about the chart."""
    ok = "ok"                      # records returned
    empty = "empty"                # 200 with no records -> "none recorded", never "none"
    unauthorized = "unauthorized"  # 401/403 -> the user's role can't see this
    error = "error"                # 4xx/5xx/unparseable
    timeout = "timeout"            # no answer within the budget, after one retry


class Record(BaseModel):
    source_ids: List[str] = Field(description="ResourceType/id of every FHIR record this was built from")
    date: Optional[str] = Field(None, description="Most relevant date, ISO 8601 (YYYY-MM-DD when only a date is known)")


class PatientRecord(Record):
    name: str
    birth_date: Optional[str] = None
    gender: Optional[str] = None


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
