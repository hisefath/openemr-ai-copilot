"""Deterministic clinical rules (the 'domain constraint enforcement' half of verification).
Rules run on normalized records, never on LLM output. Messages are fixed templates; the LLM never words a flag.
This is a deliberately small, explicit rule set for a demo, not a drug-interaction database (see ARCHITECTURE.md).

Thresholds are conservative screening cut-offs, not diagnostic criteria. OpenEMR's data has no reference
ranges or abnormal flags (AUDIT DQ-7), so critical-value thresholds are defined here."""
import re
from typing import Iterable, List, Optional

from schemas import AllergyRecord, Flag, LabRecord, MedicationRecord


# Drug classes by lowercase name fragments (generic names; brand names where common in primary care).
CLASSES = {
    "penicillin": ["penicillin", "amoxicillin", "ampicillin", "piperacillin", "nafcillin", "dicloxacillin", "augmentin"],
    "cephalosporin": ["cephalexin", "cefalexin", "cefazolin", "ceftriaxone", "cefuroxime", "cefdinir", "cefpodoxime"],
    "sulfonamide antibiotic": ["sulfamethoxazole", "sulfadiazine", "sulfisoxazole", "bactrim", "septra"],
    "nsaid": ["ibuprofen", "naproxen", "diclofenac", "celecoxib", "ketorolac", "meloxicam", "indomethacin", "aspirin"],
    "opioid": ["codeine", "morphine", "hydrocodone", "oxycodone", "tramadol", "hydromorphone"],
    "antiplatelet or anticoagulant": ["clopidogrel", "warfarin", "apixaban", "rivaroxaban", "dabigatran", "prasugrel", "ticagrelor"],
    "ace inhibitor or arb": ["lisinopril", "enalapril", "ramipril", "benazepril", "losartan", "valsartan", "irbesartan", "olmesartan"],
    "potassium-sparing or potassium supplement": ["spironolactone", "eplerenone", "amiloride", "triamterene", "potassium chloride"],
    "statin": ["simvastatin", "atorvastatin", "rosuvastatin", "pravastatin", "lovastatin", "pitavastatin"],
    "metformin": ["metformin"],
}
# Allergy to the first class means caution with drugs in the second (cross-reactivity).
CROSS_REACTIVE = {"penicillin": ["cephalosporin"]}
ALLERGY_ALIASES = {"sulfa": "sulfonamide antibiotic", "nsaid": "nsaid"}

# LOINC -> (name, unit, low critical, high critical)
CRITICAL_LABS = {
    "6298-4": ("Potassium", "mmol/L", 3.0, 6.0), "2823-3": ("Potassium", "mmol/L", 3.0, 6.0),
    "2947-0": ("Sodium", "mmol/L", 125, 155), "2951-2": ("Sodium", "mmol/L", 125, 155),
    "2339-0": ("Glucose", "mg/dL", 54, 400), "2345-7": ("Glucose", "mg/dL", 54, 400),
    "33914-3": ("eGFR", "mL/min/{1.73_m2}", 30, None), "62238-1": ("eGFR", "mL/min/{1.73_m2}", 30, None),
    "4548-4": ("Hemoglobin A1c", "%", None, 10.0), "6301-6": ("INR", None, None, 4.0),
}
EGFR_CODES = {"33914-3", "62238-1", "48642-3", "48643-1", "69405-9"}
# Strength (from the drug name) at or above which the product itself is high-risk.
HIGH_RISK_STRENGTH_MG = {"simvastatin": 80}


def classes_of(name: str) -> List[str]:
    n = name.lower()
    return [c for c, fragments in CLASSES.items() if any(f in n for f in fragments)]


def _allergy_classes(a: AllergyRecord) -> List[str]:
    s = a.substance.lower()
    found = classes_of(s) + [cls for alias, cls in ALLERGY_ALIASES.items() if re.search(rf"\b{alias}", s)]
    return sorted(set(found))


def _active(meds: Iterable[MedicationRecord]) -> List[MedicationRecord]:
    return [m for m in meds if m.name and ("active" in m.statuses or m.status == "active")]


def _when(m: MedicationRecord) -> str:
    return f" (order from {m.date[:4]}; may no longer be current)" if m.possibly_stale and m.date else ""


def check(allergies: List[AllergyRecord], medications: List[MedicationRecord], labs: List[LabRecord],
          proposed: Optional[List[str]] = None) -> List[Flag]:
    """proposed: drug names the physician asked about (e.g. 'start amoxicillin'), checked like active meds."""
    flags: List[Flag] = []
    active_allergies = [a for a in allergies if a.clinical_status in (None, "active")]
    active = _active(medications)
    candidates = [(m.name, m.source_ids, _when(m)) for m in active] + [(p, [], " (proposed)") for p in proposed or []]

    # 1. Allergy vs drug class, including cross-reactivity.
    for a in active_allergies:
        for acls in _allergy_classes(a):
            for name, sids, note in candidates:
                dcls = classes_of(name)
                if acls in dcls:
                    flags.append(Flag(rule_id="allergy-drug-class", severity="high", source_ids=a.source_ids + sids,
                                      message=f"Allergy to {a.substance} conflicts with {name}{note} ({acls})."))
                for cross in CROSS_REACTIVE.get(acls, []):
                    if cross in dcls:
                        flags.append(Flag(rule_id="allergy-cross-reactivity", severity="medium", source_ids=a.source_ids + sids,
                                          message=f"Allergy to {a.substance}: possible cross-reactivity with {name}{note} ({cross})."))

    # 2. Drug-drug combinations.
    def pairs(c1: str, c2: str, rule_id: str, severity: str, why: str) -> None:
        for n1, s1, w1 in candidates:
            for n2, s2, w2 in candidates:
                if n1 != n2 and c1 in classes_of(n1) and c2 in classes_of(n2) and (w1 != " (proposed)" or w2 != " (proposed)"):
                    flags.append(Flag(rule_id=rule_id, severity=severity, source_ids=s1 + s2,
                                      message=f"{n1}{w1} with {n2}{w2}: {why}."))
    pairs("antiplatelet or anticoagulant", "nsaid", "bleeding-risk", "high", "increased bleeding risk")
    pairs("ace inhibitor or arb", "potassium-sparing or potassium supplement", "hyperkalemia-risk", "medium", "risk of high potassium")
    statins = [m for m in active if "statin" in classes_of(m.name)]
    if len(statins) > 1:
        flags.append(Flag(rule_id="duplicate-statin", severity="medium", source_ids=[s for m in statins for s in m.source_ids],
                          message="More than one active statin: " + ", ".join(m.name + _when(m) for m in statins) + "."))

    # 3. High-risk strength from the product name.
    for name, sids, note in candidates:
        for drug, limit in HIGH_RISK_STRENGTH_MG.items():
            mg = re.search(rf"{drug}\D*?(\d+(?:\.\d+)?)\s*mg", name.lower())
            if mg and float(mg.group(1)) >= limit:
                flags.append(Flag(rule_id="high-risk-strength", severity="high", source_ids=sids,
                                  message=f"{name}{note}: {drug} {mg.group(1)} mg is at or above the {limit} mg high-risk strength."))

    # 4. Critical lab values: latest value per test only.
    latest = {}
    for lab in sorted((x for x in labs if x.value is not None and x.loinc in CRITICAL_LABS), key=lambda x: x.date or ""):
        latest[CRITICAL_LABS[lab.loinc][0]] = lab
    for test, lab in latest.items():
        _, unit, lo, hi = CRITICAL_LABS[lab.loinc]
        if unit and lab.unit and lab.unit != unit:
            continue  # unexpected unit: don't compare numbers across units
        if (lo is not None and lab.value < lo) or (hi is not None and lab.value > hi):
            flags.append(Flag(rule_id="critical-lab", severity="high", source_ids=lab.source_ids,
                              message=f"{test} {lab.value:g} {lab.unit or ''} on {lab.date} is outside the critical range.".replace("  ", " ")))

    # 5. Metformin with eGFR < 30.
    egfr = max((x for x in labs if x.loinc in EGFR_CODES and x.value is not None), key=lambda x: x.date or "", default=None)
    if egfr and egfr.value < 30:
        for name, sids, note in candidates:
            if "metformin" in classes_of(name):
                flags.append(Flag(rule_id="metformin-low-egfr", severity="high", source_ids=sids + egfr.source_ids,
                                  message=f"{name}{note} with eGFR {egfr.value:g} on {egfr.date} (below 30)."))
    return _dedupe(flags)


def _dedupe(flags: List[Flag]) -> List[Flag]:
    seen, out = set(), []
    for f in flags:
        key = (f.rule_id, frozenset(f.source_ids), f.message if not f.source_ids else "")
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out
