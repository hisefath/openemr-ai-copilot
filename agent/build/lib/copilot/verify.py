"""Deterministic verification of Claude's AnswerPlan and rendering of the answer (ARCHITECTURE §5).
Pure functions; fails closed: any exception yields the non-AI fallback."""
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

from pydantic import BaseModel, Field

from . import render
from . import rules
from .schemas import (AnswerPlan, Coverage, Flag, Intent, LabRecord, MessageResponse, Outcome, PatientBanner,
                      PatientContext, RecordItem, RenderedLine, RenderedSection, ScopeViolation, Section)

# Fixed server text. The refusal is identical for every scope violation and never says whether another patient exists.
REFUSAL = "The Co-Pilot only answers questions about the patient open in this chart, from this chart's records."
REASON_NO_PLAN = "The assistant did not return a usable answer. Showing chart data without AI selection."
REASON_NO_VALID = "No statement in the assistant's answer could be verified against this chart. Showing chart data."
REASON_BAD_CLARIFY = "The assistant's clarifying choices could not be verified. Showing chart data."
REASON_ERROR = "The answer could not be verified. Showing chart data."
DRUG_LIMITS = "Drug checks cover a small rule set; brand names and misspellings are not recognized."  # §5 limitations
MODEL_TERM_UNCHECKED = "A drug suggested by the assistant is not in the rule set and was not checked."
NO_IDENTITY = "Patient identity could not be loaded: confirm the chart before acting."  # §2 banner check
MALFORMED_ID = "malformed"  # audit marker for a cited id that isn't ResourceType/id: never the raw model text
MAX_TERM = 60  # characters of an unchecked drug term echoed back
_FHIR_ID = re.compile(r"[A-Z][A-Za-z]+/[A-Za-z0-9.\-]{1,64}")
# Week 2 cites two things that are not FHIR records: a field on an uploaded document, and a chunk of guideline
# text. Their ids are namespaced by render.citation_key so they cannot collide with a record id, and so the shape
# itself says which closed set the id has to be a member of. Shape is necessary and never sufficient — an id of
# the right shape that the server did not put in the index is still denied.
_W2_ID = re.compile(r"doc:[A-Za-z0-9.\-]{1,64}:[A-Za-z0-9._\[\]\-]{1,96}|guideline:[A-Za-z0-9._\-]{1,64}")
_DRUG_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9-]{2,30}")  # a model term is echoed only as one word (SEC-M2)
# The server, not the model, decides that a question is about giving a drug: it then gets every allergy (§5 step 4).
_PRESCRIBING = re.compile(r"\b(?:start|starting|give|giving|prescrib\w*|add|adding|switch\w*|restart\w*|resum\w*|"
                          r"safe|safely|safety|allerg\w*|interact\w*|contraindicat\w*|dose|doses|dosing)\b", re.I)


class VerifiedAnswer(BaseModel):
    """MessageResponse without correlation_id, plus what main.py needs for audit and metrics.
    Build the browser response with .response(); denied ids and the error bit never reach the browser."""
    outcome: Outcome
    patient_banner: PatientBanner
    sections: List[RenderedSection] = Field(default_factory=list)
    flags: List[Flag] = Field(default_factory=list)
    coverage: List[Coverage] = Field(default_factory=list)
    clarify: List[RenderedLine] = Field(default_factory=list)
    withheld_count: int = 0
    notice: Optional[str] = None
    data_as_of: str
    denied_source_ids: List[str] = Field(default_factory=list,
                                         description="Cited ids not in this session's index: one `denied` audit row each")
    verifier_error: bool = Field(False, description="The verifier raised; count as an error metric (FM-08)")
    handoffs: List[Any] = Field(default_factory=list, description="Week 2 routing record for this turn")
    evidence: List[Any] = Field(default_factory=list, description="Week 2 guideline evidence above the floor")

    def response(self, correlation_id: str) -> MessageResponse:
        return MessageResponse(correlation_id=correlation_id,
                               **self.model_dump(exclude={"denied_source_ids", "verifier_error"}))


def _as_of(ctx: PatientContext, now: datetime) -> str:
    return ctx.fetched_at or now.isoformat(timespec="seconds")


def _notice(ctx: PatientContext, *parts: Optional[str]) -> Optional[str]:
    return " ".join([p for p in parts if p] + ([] if ctx.patient.records else [NO_IDENTITY])) or None


def _unchecked(terms: Sequence[str]) -> List[str]:
    return [f"{t}: not in rule set, not checked." for t in dict.fromkeys(x.strip()[:MAX_TERM].lower() for x in terms
                                                                        if x.strip())]


def _with_drugs(ctx: PatientContext, flags: List[Flag], drugs: Sequence[str]) -> List[Flag]:
    if not drugs:
        return flags
    extra = rules.check(ctx.allergies.records, ctx.medications.records, ctx.labs.records, list(dict.fromkeys(drugs)))
    return flags + [f for f in extra if f not in flags]


def _denied(plan: Optional[AnswerPlan], selected: Optional[str], index: dict,
            w2_index: Optional[dict] = None) -> List[str]:
    """Cited ids outside this session's index, one `denied` audit row each (FM-09). Only well-formed FHIR ids pass
    through; anything else is model or chart text and becomes a fixed marker (no PHI in audit rows).

    `w2_index` holds the document fields and guideline chunks this turn actually retrieved, built by the server
    in render.evidence_index. Membership is still what decides: widening the accepted SHAPES to cover Week 2 ids
    without also widening the INDEX would have turned the citation gate into a regex, which is precisely the
    failure it exists to prevent."""
    known = index if not w2_index else {**index, **w2_index}
    cited = [] if plan is None else [i.source_id for i in plan.items if isinstance(i, RecordItem)]
    cited += plan.clarify.candidate_source_ids if plan and plan.clarify else []
    cited += [selected] if selected else []
    return [s if (_FHIR_ID.fullmatch(s) or _W2_ID.fullmatch(s)) else MALFORMED_ID
            for s in dict.fromkeys(cited) if s not in known]


def fallback(ctx: PatientContext, flags: Sequence[Flag], reason: str, today: date, now: datetime) -> VerifiedAnswer:
    """Non-AI answer (§5 step 6, FM-06/07/08): every allergy, active medications, active problems, the latest result
    per lab in the last 12 months, flags, coverage and one fixed reason line."""
    def lines(kind: str, records: Sequence) -> List[RenderedLine]:
        return [render.render_record(kind, r, today, flags, mark_unclassified=True) for r in records]

    cutoff = (today - timedelta(days=365)).isoformat()
    latest: Dict[str, LabRecord] = {}
    for lab in sorted(ctx.labs.records, key=lambda x: x.date or ""):
        if lab.value is not None and (lab.date or "") >= cutoff:
            latest[lab.loinc or lab.name] = lab
    groups = [
        (Section.safety, lines("allergies", ctx.allergies.records)),
        (Section.background,
         lines("medications", [m for m in ctx.medications.records if m.status == "active" or "active" in m.statuses])
         + lines("conditions", [c for c in ctx.conditions.records if c.clinical_status == "active"])),
        (Section.recent_results, lines("labs", sorted(latest.values(), key=lambda x: x.date, reverse=True))),
    ]
    as_of = _as_of(ctx, now)
    return VerifiedAnswer(outcome=Outcome.fail, patient_banner=render.patient_banner(ctx, today),
                          sections=[RenderedSection(section=s, lines=ls) for s, ls in groups if ls], flags=list(flags),
                          coverage=render.coverage(ctx, as_of), notice=_notice(ctx, reason), data_as_of=as_of)


def verify_and_render(plan: Optional[AnswerPlan], ctx: PatientContext, question: str,
                      selected_source_id: Optional[str], flags_base: Sequence[Flag], today: date,
                      now: datetime, w2_index: Optional[dict] = None) -> VerifiedAnswer:
    """§5 steps 1-6 in order. plan=None (timeout, refusal, truncation, unparseable) goes straight to the fallback.
    Denied ids and flags for drugs named in the question are computed first, so a refusal and the fail-closed path
    (FM-08) keep them; if that step itself raises, flags_base is used."""
    denied: List[str] = []
    flags, notes = list(flags_base), []
    try:
        index = render.record_index(ctx)
        denied = _denied(plan, selected_source_id, index, w2_index)
        found, unknown = rules.drugs_in_text(question)
        flags = _with_drugs(ctx, flags, found)
        notes = _unchecked(unknown)
        return _verify(plan, ctx, question, selected_source_id, index, flags, notes, denied, today, now)
    except Exception:  # fail closed; no exception text leaves this function
        answer = fallback(ctx, flags, " ".join([REASON_ERROR, *notes, DRUG_LIMITS]), today, now)
        return answer.model_copy(update={"verifier_error": True, "denied_source_ids": denied})


def _verify(plan: Optional[AnswerPlan], ctx: PatientContext, question: str, selected_source_id: Optional[str],
            index: dict, flags: List[Flag], notes: List[str], denied: List[str], today: date,
            now: datetime) -> VerifiedAnswer:
    as_of = _as_of(ctx, now)
    base = dict(patient_banner=render.patient_banner(ctx, today), coverage=render.coverage(ctx, as_of),
                data_as_of=as_of, denied_source_ids=denied)

    # 1 Scope: fixed refusal, no records rendered. Flags (including drugs named in the question) are about this chart
    # and always render; the model's proposed_drugs are not used.
    if plan is not None and plan.scope_violation != ScopeViolation.none:
        return VerifiedAnswer(outcome=Outcome.refused, flags=flags, notice=REFUSAL, **base)

    # 4 Rules, model half: proposed drugs mapped through the same dictionary. An unmatched term is echoed only if it is
    # one word or the physician's own text; anything else gets one fixed line (Claude selects; the server speaks).
    found: List[str] = []
    for term in plan.proposed_drugs if plan else []:
        hits, _ = rules.drugs_in_text(term)
        found += hits
        t = term.strip()
        if t and not hits:
            notes = notes + (_unchecked([t]) if _DRUG_TOKEN.fullmatch(t) or t.lower() in question.lower()
                             else [MODEL_TERM_UNCHECKED])
    notes = list(dict.fromkeys(notes))
    flags = _with_drugs(ctx, flags, found)
    drug_question = any(rules.drugs_in_text(question)) or bool(_PRESCRIBING.search(question))

    def fail(reason: str, withheld: int) -> VerifiedAnswer:
        answer = fallback(ctx, flags, " ".join([reason, *notes, DRUG_LIMITS]), today, now)
        return answer.model_copy(update={"withheld_count": withheld, "denied_source_ids": denied})

    if plan is None:
        return fail(REASON_NO_PLAN, 0)
    # Every allergy, unclassified ones marked, whenever the model OR the server sees a drug question (§5 step 4).
    safety = plan.intent == Intent.safety_check or bool(plan.proposed_drugs) or drug_question
    notice = _notice(ctx, *notes, DRUG_LIMITS if safety else None)
    selected = selected_source_id if selected_source_id in index else None

    # 5 Clarify: only when the physician hasn't just picked a chip; all candidates must be valid; items are ignored.
    if plan.clarify and not selected:
        candidates = plan.clarify.candidate_source_ids
        invalid = [c for c in candidates if c not in index]
        if invalid:
            return fail(REASON_BAD_CLARIFY, len(invalid))
        chips = {id(index[c][1]): render.render_record(*index[c], today, flags) for c in candidates}
        if len(chips) > 1:
            return VerifiedAnswer(outcome=Outcome.clarify, flags=flags, clarify=list(chips.values()), notice=notice,
                                  **base)
        # Every candidate is one merged record: nothing to choose, so show it alone (items still ignored).
        plan, selected = plan.model_copy(update={"items": []}), candidates[0]

    # 2 Attribution and 3 Trends. The chip the physician picked is shown first unless the plan already cites it.
    items = list(plan.items)
    picked = index[selected][1] if selected else None
    if picked and not any(isinstance(i, RecordItem) and i.source_id in index and index[i.source_id][1] is picked
                          for i in items):
        items.insert(0, RecordItem(source_id=selected, section=Section.visit_context))
    by_section: Dict[Section, List[RenderedLine]] = {}
    seen, valid, withheld = set(), 0, 0
    for item in items:
        if isinstance(item, RecordItem):
            if item.source_id not in index:
                withheld += 1
                continue
            kind, rec = index[item.source_id]
            valid += 1
            if id(rec) in seen:  # merged medication cited by two of its ids, or a repeat
                continue
            seen.add(id(rec))
            line = render.render_record(kind, rec, today, flags, mark_unclassified=safety)
        else:
            line = render.render_trend(ctx.labs.records, item.lab, today)
            if line is None:
                withheld += 1
                continue
            valid += 1
            if tuple(line.source_ids) in seen:  # same series asked for twice
                continue
            seen.add(tuple(line.source_ids))
        by_section.setdefault(item.section, []).append(line)

    # 6 Outcome.
    if not valid:
        return fail(REASON_NO_VALID, withheld)
    if safety:  # every allergy entry in every safety answer, unclassified ones marked (§5 step 4)
        by_section.setdefault(Section.safety, []).extend(
            render.render_record("allergies", a, today, flags, mark_unclassified=True)
            for a in ctx.allergies.records if id(a) not in seen)
    return VerifiedAnswer(outcome=Outcome.pass_with_removals if withheld else Outcome.passed,
                          sections=[RenderedSection(section=s, lines=by_section[s]) for s in Section if by_section.get(s)],
                          flags=flags, withheld_count=withheld, notice=notice, **base)
