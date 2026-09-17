"""Read-only FHIR tools. Each call uses the caller's own bearer token (SMART user/ scopes),
so OpenEMR's access control decides what is visible. Never a system/ token."""
import os
from typing import List, Optional

import httpx
from langfuse import observe
from pydantic import BaseModel, Field

FHIR_BASE = os.environ.get("OPENEMR_FHIR_BASE", "").rstrip("/")
PATIENT_ID = Field(pattern=r"^[A-Za-z0-9\-\.]{1,64}$")


class PatientInput(BaseModel):
    patient_id: str = PATIENT_ID


class PatientOut(BaseModel):
    source_id: str
    name: str
    birth_date: Optional[str] = None
    gender: Optional[str] = None


class AllergyOut(BaseModel):
    source_id: str
    substance: str
    criticality: Optional[str] = None
    reactions: List[str] = []


class MedicationOut(BaseModel):
    source_id: str
    medication: str
    status: Optional[str] = None
    dosage: Optional[str] = None
    authored_on: Optional[str] = None


def _text(cc: Optional[dict]) -> str:
    """Best human-readable label from a FHIR CodeableConcept."""
    if not cc:
        return "unknown"
    return cc.get("text") or next((c.get("display") for c in cc.get("coding", []) if c.get("display")), "unknown")


async def _get(http: httpx.AsyncClient, token: str, path: str, params: Optional[dict] = None) -> dict:
    r = await http.get(f"{FHIR_BASE}/{path}", params=params, headers={"Authorization": token, "Accept": "application/fhir+json"})
    r.raise_for_status()
    return r.json()


def _entries(bundle: dict) -> List[dict]:
    return [e["resource"] for e in bundle.get("entry", []) if "resource" in e]


@observe(as_type="tool", capture_input=False)  # input includes the bearer token
async def get_patient(http: httpx.AsyncClient, token: str, inp: PatientInput) -> List[PatientOut]:
    p = await _get(http, token, f"Patient/{inp.patient_id}")
    n = (p.get("name") or [{}])[0]
    name = n.get("text") or " ".join(n.get("given", []) + [n.get("family", "")]).strip() or "unknown"
    return [PatientOut(source_id=f"Patient/{p['id']}", name=name, birth_date=p.get("birthDate"), gender=p.get("gender"))]


@observe(as_type="tool", capture_input=False)  # input includes the bearer token
async def get_allergies(http: httpx.AsyncClient, token: str, inp: PatientInput) -> List[AllergyOut]:
    bundle = await _get(http, token, "AllergyIntolerance", {"patient": inp.patient_id})
    return [
        AllergyOut(
            source_id=f"AllergyIntolerance/{a['id']}",
            substance=_text(a.get("code")),
            criticality=a.get("criticality"),
            reactions=[_text(m) for r in a.get("reaction", []) for m in r.get("manifestation", [])],
        )
        for a in _entries(bundle)
    ]


@observe(as_type="tool", capture_input=False)  # input includes the bearer token
async def get_medications(http: httpx.AsyncClient, token: str, inp: PatientInput) -> List[MedicationOut]:
    bundle = await _get(http, token, "MedicationRequest", {"patient": inp.patient_id})
    return [
        MedicationOut(
            source_id=f"MedicationRequest/{m['id']}",
            medication=_text(m.get("medicationCodeableConcept")),
            status=m.get("status"),
            dosage=(m.get("dosageInstruction") or [{}])[0].get("text"),
            authored_on=m.get("authoredOn"),
        )
        for m in _entries(bundle)
    ]


TOOLS = {"get_patient": get_patient, "get_allergies": get_allergies, "get_medications": get_medications}

TOOL_SPECS = [
    {
        "name": name,
        "description": desc,
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"patient_id": {"type": "string", "description": "FHIR Patient id of the current chart"}},
            "required": ["patient_id"],
            "additionalProperties": False,
        },
    }
    for name, desc in [
        ("get_patient", "Demographics for the current patient: name, birth date, gender."),
        ("get_allergies", "The current patient's recorded allergies and intolerances, with reactions."),
        ("get_medications", "The current patient's medication orders (MedicationRequest), with status and dosage."),
    ]
]
