"""Turns OpenEMR FHIR R4 bundles into cited, deduplicated records.
Each rule below handles a data problem observed in real OpenEMR output (see AUDIT.md)."""
import re
from datetime import date
from typing import Dict, List, Optional

from schemas import (AllergyRecord, ConditionRecord, EncounterRecord, LabRecord, MedicationRecord, PatientRecord,
                     VitalRecord)

PLACEHOLDER_VALUES = {"{entry.value}"}  # unrendered CCDA template text stored as a lab result
SEVERITIES = {"mild", "moderate", "severe", "mild to moderate", "moderate to severe"}
STALE_ACTIVE_YEARS = 5
_TAG = re.compile(r"\((disorder|finding|situation|procedure|substance|organism|event|morphologic abnormality)\)\s*$")


def resources(bundle: dict) -> List[dict]:
    return [e["resource"] for e in (bundle or {}).get("entry") or [] if isinstance(e, dict) and e.get("resource")]


def _text(v: Optional[str]) -> Optional[str]:
    v = (v or "").strip()
    return v or None


def _first_coding(cc: Optional[dict]) -> dict:
    codings = (cc or {}).get("coding") or []
    return codings[0] if codings and isinstance(codings[0], dict) else {}


def _label(cc: Optional[dict]) -> Optional[str]:
    return _text((cc or {}).get("text")) or _text(_first_coding(cc).get("display"))


def _narrative(resource: dict) -> Optional[str]:
    div = (resource.get("text") or {}).get("div") if isinstance(resource.get("text"), dict) else resource.get("text")
    return _text(re.sub(r"<[^>]+>", " ", div or ""))


def _code(cc: Optional[dict]) -> Optional[str]:
    return _text(_first_coding(cc).get("code"))


def _day(value: Optional[str]) -> Optional[str]:
    return value[:10] if value else None


def _sid(resource: dict) -> str:
    return f"{resource.get('resourceType')}/{resource.get('id')}"


def patient(bundle: dict) -> List[PatientRecord]:
    out = []
    for p in resources(bundle):
        n = (p.get("name") or [{}])[0]
        name = _text(n.get("text")) or " ".join(filter(None, [*(n.get("given") or []), n.get("family")])) or "Name not recorded"
        out.append(PatientRecord(source_ids=[_sid(p)], name=name, birth_date=p.get("birthDate"), gender=p.get("gender")))
    return out


def allergies(bundle: dict) -> List[AllergyRecord]:
    out = []
    for a in resources(bundle):
        verification = _code(a.get("verificationStatus"))
        if verification == "entered-in-error":
            continue
        code = _code(a.get("code"))
        coded = bool(code) and code.lower() != "unknown"
        substance = (_label(a.get("code")) if coded else None) or _narrative(a) or "Substance not recorded"
        labels = [r for x in a.get("reaction") or [] for m in x.get("manifestation") or []
                  if (r := _label(m))]  # "" manifestations mean nothing recorded
        # OpenEMR puts the severity (Mild/Moderate/Severe) where the reaction symptom belongs.
        severity = next((x.lower() for x in labels if x.lower() in SEVERITIES), None)
        reactions = [x for x in labels if x.lower() not in SEVERITIES]
        out.append(AllergyRecord(source_ids=[_sid(a)], date=_day(a.get("recordedDate")), substance=substance, coded=coded,
                                 clinical_status=_code(a.get("clinicalStatus")), verification_status=verification,
                                 criticality=a.get("criticality"), severity=severity, reactions=reactions))
    return out


def medications(bundle: dict, today: Optional[date] = None) -> List[MedicationRecord]:
    """OpenEMR returns each med twice: an 'order' row with the coded drug, and a 'plan' row whose drug has only a
    name and whose RxNorm code sits in reasonCode (AUDIT DQ-M3). Fold plan rows into their order row by that code."""
    today = today or date.today()
    by_code: Dict[str, MedicationRecord] = {}
    out: List[MedicationRecord] = []
    orphans: List[dict] = []
    for m in resources(bundle):
        cc = m.get("medicationCodeableConcept") or {}
        if not _code(cc) and m.get("reasonCode"):
            orphans.append(m)
            continue
        dosage = "; ".join(t for d in m.get("dosageInstruction") or [] if isinstance(d, dict) and (t := _text(d.get("text"))))
        status = m.get("status")
        authored = _day(m.get("authoredOn"))
        stale = status == "active" and bool(authored) and int(authored[:4]) <= today.year - STALE_ACTIVE_YEARS
        rec = MedicationRecord(source_ids=[_sid(m)], date=authored, name=_label(cc), rxnorm=_code(cc), status=status,
                               dosage=dosage or None, statuses=[status] if status else [], possibly_stale=stale)
        if rec.rxnorm and rec.rxnorm in by_code:  # same drug twice as orders
            _merge(by_code[rec.rxnorm], m)
            continue
        if rec.rxnorm:
            by_code[rec.rxnorm] = rec
        out.append(rec)
    for m in orphans:
        codes = [c.get("code") for r in m.get("reasonCode") or [] for c in (r.get("coding") or []) if c.get("code")]
        target = next((by_code[c] for c in codes if c in by_code), None)
        if target:
            _merge(target, m)
        else:
            out.append(MedicationRecord(source_ids=[_sid(m)], date=_day(m.get("authoredOn")),
                                        name=_label(m.get("medicationCodeableConcept")),
                                        rxnorm=codes[0] if codes else None, status=m.get("status"),
                                        statuses=[m.get("status")] if m.get("status") else []))
    return out


def _merge(rec: MedicationRecord, row: dict) -> None:
    rec.source_ids.append(_sid(row))
    if row.get("status") and row["status"] not in rec.statuses:
        rec.statuses.append(row["status"])
    rec.status_conflict = len(rec.statuses) > 1


def conditions(bundle: dict) -> List[ConditionRecord]:
    out = []
    for c in resources(bundle):
        if _code(c.get("verificationStatus")) == "entered-in-error":
            continue
        name = _label(c.get("code")) or _narrative(c) or "Condition name not recorded"
        tag = _TAG.search(name)
        out.append(ConditionRecord(source_ids=[_sid(c)], date=_day(c.get("recordedDate") or c.get("onsetDateTime")),
                                   name=name, clinical_status=_code(c.get("clinicalStatus")),
                                   kind=tag.group(1) if tag else None, onset=_day(c.get("onsetDateTime")),
                                   abatement=_day(c.get("abatementDateTime"))))
    return out


def labs(bundle: dict) -> List[LabRecord]:
    out = []
    for o in resources(bundle):
        if o.get("status") == "entered-in-error":
            continue
        vq = o.get("valueQuantity") or {}
        value = vq.get("value") if isinstance(vq.get("value"), (int, float)) else None
        if value is None and _text(o.get("valueString")) and o["valueString"] not in PLACEHOLDER_VALUES:
            try:
                value = float(o["valueString"])
            except ValueError:
                pass
        interp = _label((o.get("interpretation") or [None])[0]) or _code((o.get("interpretation") or [None])[0])
        out.append(LabRecord(source_ids=[_sid(o)], date=_day(o.get("effectiveDateTime")), name=_label(o.get("code")) or "Lab",
                             loinc=_code(o.get("code")), value=value, unit=_text(vq.get("unit")) if value is not None else None,
                             status=o.get("status"), abnormal_flag=interp))
    return out


# Values outside these ranges for the recorded unit are almost certainly a unit error (e.g. cm stored as [in_i]).
_PLAUSIBLE = {"[in_i]": (10, 96), "in_i": (10, 96), "cm": (25, 245), "kg": (0.5, 400), "[lb_av]": (1, 900), "lb_av": (1, 900)}


def vitals(bundle: dict) -> List[VitalRecord]:
    out = []
    for o in resources(bundle):
        vq = o.get("valueQuantity") or {}
        if o.get("dataAbsentReason") or not isinstance(vq.get("value"), (int, float)):
            continue  # panels and data-absent components carry no reading (AUDIT DQ-8)
        unit = _text(vq.get("code")) or _text(vq.get("unit"))
        lo, hi = _PLAUSIBLE.get(unit or "", (float("-inf"), float("inf")))
        out.append(VitalRecord(source_ids=[_sid(o)], date=_day(o.get("effectiveDateTime")), name=_label(o.get("code")) or "Vital",
                               loinc=_code(o.get("code")), value=vq["value"], unit=_text(vq.get("unit")),
                               unit_suspect=not lo <= vq["value"] <= hi))
    return out


def encounters(bundle: dict) -> List[EncounterRecord]:
    out = []
    for e in resources(bundle):
        out.append(EncounterRecord(source_ids=[_sid(e)], date=_day((e.get("period") or {}).get("start")),
                                   type=_label((e.get("type") or [None])[0]),
                                   reason=_label((e.get("reasonCode") or [None])[0])))
    return sorted(out, key=lambda r: r.date or "", reverse=True)
