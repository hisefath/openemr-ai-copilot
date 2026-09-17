"""Normalization against real OpenEMR FHIR output (tests/fixtures, synthetic patients) plus hand-built edge cases.
Each test names the failure mode it guards against."""
import json
from datetime import date
from pathlib import Path

import normalize as n

FIX = Path(__file__).parent / "fixtures"
A = json.loads((FIX / "patient_a.json").read_text())
B = json.loads((FIX / "patient_b.json").read_text())
TODAY = date(2026, 9, 17)


def test_meds_order_and_plan_rows_fold_into_one_record():
    """Guards: every med listed twice, half with no name and today's date (AUDIT DQ-M3)."""
    meds = n.medications(A["MedicationRequest"], TODAY)
    assert len(n.resources(A["MedicationRequest"])) == 22
    assert len(meds) == 11
    assert all(m.name for m in meds)
    assert all(len(m.source_ids) == 2 for m in meds)
    assert len(n.medications(B["MedicationRequest"], TODAY)) == 10


def test_ancient_active_med_is_marked_possibly_stale():
    """Guards: stating a 1968 order as a current medication (AUDIT DQ-3)."""
    diphen = next(m for m in n.medications(A["MedicationRequest"], TODAY) if "diphenhydramine" in m.name.lower())
    assert diphen.status == "active" and diphen.possibly_stale


def test_malformed_dosage_instruction_does_not_crash_or_invent_dosage():
    """Guards: OpenEMR serializes dosageInstruction as [[]] (invalid FHIR)."""
    assert all(m.dosage is None for m in n.medications(A["MedicationRequest"], TODAY))


def test_empty_reactions_and_severity_in_reaction_field():
    """Guards: manifestation.text == '' shown as a reaction, and 'Moderate' (a severity OpenEMR stores in the
    reaction field) shown as the patient's reaction."""
    alls = n.allergies(A["AllergyIntolerance"])
    assert len(alls) == 12
    assert any(a.substance.startswith("Latex") for a in alls)
    assert all(a.reactions == [] for a in alls)
    assert {a.severity for a in alls} == {None, "mild", "moderate"}
    assert next(a for a in alls if a.substance.startswith("Peanut")).severity == "moderate"


def test_no_allergy_rows_normalize_to_empty_list():
    """Guards: an empty bundle crashing or producing a fake record (status 'empty' is decided by the fetcher)."""
    assert n.allergies(B["AllergyIntolerance"]) == []


def test_lab_placeholder_values_are_not_results():
    """Guards: citing the literal '{entry.value}' template text as a lab result."""
    labs = n.labs(A["Observation_laboratory"]) + n.labs(B["Observation_laboratory"])
    raw = n.resources(A["Observation_laboratory"]) + n.resources(B["Observation_laboratory"])
    placeholders = {f"Observation/{o['id']}" for o in raw if o.get("valueString") == "{entry.value}"}
    assert placeholders
    assert all(lab.value is None and lab.unit is None for lab in labs if lab.source_ids[0] in placeholders)
    assert sum(lab.value is not None for lab in labs) > 100


def test_missing_interpretation_is_not_normal():
    """Guards: treating 'no abnormal flag' as 'normal' (AUDIT DQ-7)."""
    assert all(lab.abnormal_flag is None for lab in n.labs(B["Observation_laboratory"]))


def test_vitals_drop_data_absent_and_flag_impossible_units():
    """Guards: citing empty panels, and reporting height 163 inches (cm stored as [in_i])."""
    vit = n.vitals(A["Observation_vital_signs"])
    assert vit and all(isinstance(v.value, (int, float)) for v in vit)
    height = [v for v in vit if v.name == "Body height"]
    assert height and all(v.unit_suspect for v in height if v.value > 96)
    assert not any(v.unit_suspect for v in vit if v.name == "Heart rate")


def test_conditions_carry_status_and_semantic_kind():
    """Guards: presenting inactive problems or social 'findings' as active diagnoses."""
    conds = n.conditions(B["Condition"])
    assert len(conds) == 71
    assert {c.clinical_status for c in conds} == {"active", "inactive"}
    assert any(c.kind == "finding" for c in conds)


def test_encounters_newest_first():
    """Guards: 'last visit' picking an old encounter."""
    enc = n.encounters(A["Encounter"])
    assert [e.date for e in enc] == sorted([e.date for e in enc], reverse=True)


def test_uncoded_allergy_uses_narrative_name_and_is_marked_uncoded():
    """Guards: free-text 'Penicillin' allergy shown as substance 'Unknown' (AUDIT DQ-2)."""
    bundle = {"entry": [{"resource": {
        "resourceType": "AllergyIntolerance", "id": "x1",
        "code": {"coding": [{"code": "unknown", "display": "Unknown"}]},
        "text": {"div": "<div xmlns='http://www.w3.org/1999/xhtml'>Penicillin</div>"},
        "clinicalStatus": {"coding": [{"code": "active"}]}}}]}
    [rec] = n.allergies(bundle)
    assert rec.substance == "Penicillin" and rec.coded is False


def test_entered_in_error_records_are_excluded():
    """Guards: retracted allergies and conditions shown as current."""
    err = {"coding": [{"code": "entered-in-error"}]}
    assert n.allergies({"entry": [{"resource": {"resourceType": "AllergyIntolerance", "id": "1", "verificationStatus": err,
                                                "code": {"text": "Sulfa"}}}]}) == []
    assert n.conditions({"entry": [{"resource": {"resourceType": "Condition", "id": "2", "verificationStatus": err,
                                                 "code": {"text": "Diabetes"}}}]}) == []


def test_conflicting_statuses_for_same_drug_are_flagged_not_resolved():
    """Guards: silently picking 'active' or 'completed' when the chart disagrees with itself (AUDIT DQ-3)."""
    rx = {"coding": [{"system": "http://www.nlm.nih.gov/research/umls/rxnorm", "code": "860975", "display": "Metformin"}]}
    bundle = {"entry": [
        {"resource": {"resourceType": "MedicationRequest", "id": "o", "intent": "order", "status": "completed",
                      "medicationCodeableConcept": rx, "authoredOn": "2024-01-01"}},
        {"resource": {"resourceType": "MedicationRequest", "id": "p", "intent": "plan", "status": "active",
                      "reasonCode": [{"coding": [{"code": "860975"}]}]}}]}
    [m] = n.medications(bundle, TODAY)
    assert m.status_conflict and set(m.statuses) == {"completed", "active"}
    assert m.source_ids == ["MedicationRequest/o", "MedicationRequest/p"]


def test_reordered_drug_takes_the_latest_order_date_and_staleness():
    """Guards: a current prescription labeled 'may no longer be current' because an older order of the same
    drug was seen first (found with a seeded clopidogrel reorder on top of a 2020 Synthea order)."""
    rx = {"coding": [{"system": "http://www.nlm.nih.gov/research/umls/rxnorm", "code": "309362", "display": "Clopidogrel 75 MG"}]}
    old = {"resourceType": "MedicationRequest", "id": "old", "intent": "order", "status": "active",
           "medicationCodeableConcept": rx, "authoredOn": "2020-03-12T00:00:00+00:00"}
    new = {"resourceType": "MedicationRequest", "id": "new", "intent": "order", "status": "active",
           "medicationCodeableConcept": rx, "authoredOn": "2026-06-19T00:00:00+00:00",
           "dosageInstruction": [{"text": "1 tablet by mouth daily"}]}
    for order in ([old, new], [new, old]):
        [m] = n.medications({"entry": [{"resource": r} for r in order]}, TODAY)
        assert m.date == "2026-06-19" and not m.possibly_stale and m.dosage == "1 tablet by mouth daily"
        assert set(m.source_ids) == {"MedicationRequest/old", "MedicationRequest/new"}


def test_malformed_bundles_do_not_crash():
    """Guards: a 200 with an odd body taking down the whole briefing."""
    for fn in (n.patient, n.allergies, n.medications, n.conditions, n.labs, n.vitals, n.encounters):
        assert fn({}) == [] and fn({"entry": None}) == [] and fn({"entry": ["junk", {}]}) == []
