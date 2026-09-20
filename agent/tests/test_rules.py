"""Domain rules. Each test names the failure mode it guards against. Positive AND negative cases: a false alarm
between rooms costs trust as surely as a miss costs safety."""
import json
from datetime import date
from pathlib import Path

from copilot import normalize as n
from copilot import rules
from copilot.schemas import AllergyRecord, LabRecord, MedicationRecord

FIX = Path(__file__).parent / "fixtures"
A = json.loads((FIX / "patient_a.json").read_text())
B = json.loads((FIX / "patient_b.json").read_text())
TODAY = date(2026, 9, 17)


def med(name, status="active", sid="MedicationRequest/m", stale=False, year="2025"):
    return MedicationRecord(source_ids=[sid], name=name, status=status, statuses=[status], possibly_stale=stale, date=f"{year}-01-01")


def allergy(substance, status="active", sid="AllergyIntolerance/a"):
    return AllergyRecord(source_ids=[sid], substance=substance, coded=False, clinical_status=status)


def lab(loinc, value, unit, day="2026-08-01", sid="Observation/l"):
    return LabRecord(source_ids=[sid], name=loinc, loinc=loinc, value=value, unit=unit, date=day)


def ids(flags):
    return sorted(f.rule_id for f in flags)


def test_real_sulfa_allergy_blocks_proposed_bactrim():
    """Guards: UC2 'safe to start Bactrim?' missing the patient's recorded sulfamethoxazole allergy."""
    flags = rules.check(n.allergies(A["AllergyIntolerance"]), n.medications(A["MedicationRequest"], TODAY), [],
                        proposed=["sulfamethoxazole/trimethoprim"])
    hit = [f for f in flags if f.rule_id == "allergy-drug-class"]
    assert hit and hit[0].severity == "high"
    assert any(s.startswith("AllergyIntolerance/") for s in hit[0].source_ids)


def test_real_patient_b_completed_ibuprofen_does_not_flag_bleeding():
    """Guards: false alarm from a completed NSAID next to active clopidogrel."""
    flags = rules.check([], n.medications(B["MedicationRequest"], TODAY), n.labs(B["Observation_laboratory"]))
    assert "bleeding-risk" not in ids(flags)


def test_real_patient_b_proposed_nsaid_with_active_clopidogrel_flags_bleeding():
    """Guards: UC2 'can I give ibuprofen?' missing active clopidogrel."""
    flags = rules.check([], n.medications(B["MedicationRequest"], TODAY), [], proposed=["ibuprofen 400 mg"])
    bleed = [f for f in flags if f.rule_id == "bleeding-risk"]
    assert bleed and "Clopidogrel" in bleed[0].message


def test_penicillin_allergy_flags_amoxicillin_and_cephalosporin_cross_reactivity():
    """Guards: missing a direct class conflict or the penicillin -> cephalosporin caution."""
    flags = rules.check([allergy("Penicillin")], [med("Amoxicillin 500 MG"), med("Cephalexin 500 MG", sid="MedicationRequest/c")], [])
    assert ids(flags) == ["allergy-cross-reactivity", "allergy-drug-class"]


def test_inactive_allergy_and_unrelated_drugs_do_not_flag():
    """Guards: false alarms from resolved allergies or name collisions."""
    assert rules.check([allergy("Penicillin", status="inactive")], [med("Amoxicillin 500 MG")], []) == []
    assert rules.check([allergy("Peanut (substance)"), allergy("Latex (substance)")], [med("Lisinopril 10 MG")], []) == []


def test_stale_active_med_still_flags_but_says_it_may_not_be_current():
    """Guards: either silently ignoring or overstating a decades-old 'active' order."""
    [f] = rules.check([allergy("Naproxen")], [med("Naproxen sodium 220 MG", stale=True, year="2007")], [])
    assert "may no longer be current" in f.message and "2007" in f.message


def test_duplicate_active_statins_flag_but_completed_one_does_not():
    """Guards: missing therapeutic duplication, or flagging a statin that was switched."""
    assert ids(rules.check([], [med("Simvastatin 10 MG"), med("Atorvastatin 20 MG", sid="MedicationRequest/x")], [])) == ["duplicate-statin"]
    assert rules.check([], [med("Simvastatin 10 MG", status="completed"), med("Simvastatin 20 MG", sid="MedicationRequest/x")], []) == []


def test_high_risk_strength_from_product_name():
    """Guards: simvastatin 80 mg passing unnoticed."""
    assert ids(rules.check([], [med("Simvastatin 80 MG Oral Tablet")], [])) == ["high-risk-strength"]
    assert rules.check([], [med("Simvastatin 20 MG Oral Tablet")], []) == []


def test_critical_labs_use_latest_value_and_respect_units():
    """Guards: flagging an old abnormal value that has since normalized, or comparing across units."""
    old_high = lab("6298-4", 6.8, "mmol/L", day="2025-01-01", sid="Observation/old")
    new_normal = lab("6298-4", 4.1, "mmol/L", day="2026-08-01", sid="Observation/new")
    assert rules.check([], [], [old_high, new_normal]) == []
    [f] = rules.check([], [], [new_normal, lab("6298-4", 6.4, "mmol/L", day="2026-09-01", sid="Observation/k")])
    assert f.rule_id == "critical-lab" and f.source_ids == ["Observation/k"]
    assert rules.check([], [], [lab("2339-0", 25.0, "mmol/L")]) == []  # glucose in mmol/L: not compared to mg/dL limits


def test_metformin_with_low_egfr():
    """Guards: missing a contraindication that needs a lab and a med together."""
    assert ids(rules.check([], [med("Metformin 500 MG")], [lab("33914-3", 24.0, "mL/min/{1.73_m2}")])) == ["critical-lab", "metformin-low-egfr"]
    assert rules.check([], [med("Metformin 500 MG")], [lab("33914-3", 64.0, "mL/min/{1.73_m2}")]) == []


def test_real_patient_labs_produce_no_unit_mismatch_crash():
    """Guards: the rules engine crashing on real data (placeholders, data-absent, string units)."""
    for p in (A, B):
        rules.check(n.allergies(p["AllergyIntolerance"]), n.medications(p["MedicationRequest"], TODAY), n.labs(p["Observation_laboratory"]))
