"""One supervisor, two workers, and a record of every decision between them.

    supervisor          decides what happens next. Does not do the work.
    intake_extractor    reads the document and locates what it read. Loops on its own tools.
    evidence_retriever  finds guideline evidence. Loops on its own tools.
    answer              renders what is grounded.

Nodes are THIN. Each one calls a module that already works standalone and is already tested — extract, locate,
retrieve — so the graph owns control flow and nothing else. That is deliberate: if LangGraph turns out to be the
wrong frame, the fallback is a hand-rolled loop behind these same four functions, not a rewrite.

TWO THINGS THIS FILE IS CAREFUL ABOUT

Termination is code, not a model. Each worker loops until a condition that can be *checked* says stop: the schema
validates and every required field is located or explicitly marked unlocated; or k chunks clear the rerank floor.
A judged "am I done yet?" call costs a second and a cent every iteration, which is invisible once and ruinous at
scale — and it can be wrong, which a schema check cannot.

The supervisor is measured, not assumed. With `document`, `extracted` and `evidence` all on the state, the
routing policy is mostly three null checks, so every routing call records what the deterministic policy would
have chosen alongside what the model chose. If the model never disagrees, that is a number worth reporting and
acting on, not a fact to discover in an interview.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional

from typing_extensions import TypedDict

from . import extract as extract_mod
from .deadline import Deadline
from .documents import Pages
from .retrieve import HybridRetriever, RetrievalUnavailable
from .schemas import (DocumentRef, EvidenceChunk, HandoffRecord, RouteTarget, RoutingDecision, RoutingReason)

log = logging.getLogger("agent")

MAX_EXTRACT_ITERATIONS = 3
MAX_RETRIEVE_ITERATIONS = 2
NODE_MARGIN_S = 0.5          # never start a node that cannot finish; a half-run node is worse than a skipped one


class GraphState(TypedDict, total=False):
    session_ref: str                  # never the handle
    patient_id: str                   # fixed at launch, server-side; workers cannot change it
    question: Optional[str]
    document: Optional[DocumentRef]
    pages: Optional[Pages]
    extracted: Optional[Any]
    evidence: List[EvidenceChunk]
    prior_turn: Optional[Dict[str, Any]]
    handoffs: List[HandoffRecord]
    deadline: Deadline
    correlation_id: str
    outcome_reason: Optional[RoutingReason]


class Deps:
    """Everything the graph talks to. Injected so the whole graph runs offline in tests and in the eval gate."""

    def __init__(self, llm: Any, settings: Any, retriever: Optional[HybridRetriever]):
        self.llm, self.settings, self.retriever = llm, settings, retriever


# ---------------------------------------------------------------- routing


def deterministic_next(state: GraphState) -> RoutingDecision:
    """What a rule would choose. Used three ways: as the counterfactual the supervisor is measured against, as
    the fallback when the model call fails, and as the thing a reader can check the supervisor's answer against."""
    if state.get("outcome_reason") is RoutingReason.out_of_scope:
        return RoutingDecision(next=RouteTarget.refuse, reason=RoutingReason.out_of_scope)
    if state.get("document") is not None and state.get("extracted") is None:
        return RoutingDecision(next=RouteTarget.extract, reason=RoutingReason.no_document_extracted)
    if not state.get("evidence") and state.get("question"):
        return RoutingDecision(next=RouteTarget.retrieve, reason=RoutingReason.evidence_below_floor)
    return RoutingDecision(next=RouteTarget.answer, reason=RoutingReason.ready_to_answer)


def state_shape(state: GraphState) -> Dict[str, Any]:
    """What the supervisor is allowed to see: shape, never values.

    Document text is attacker-controlled — a chief-concern field can contain an instruction. Passing counts and
    presence flags instead of content closes the injection path and the PHI path in the same line."""
    extracted = state.get("extracted")
    return {
        "has_question": bool(state.get("question")),
        "document": "present" if state.get("document") else "absent",
        "extracted": ("absent" if extracted is None
                      else f"{len(extract_mod._citations(extracted))} fields"),
        "evidence": f"{len(state.get('evidence') or [])} chunks",
        "has_prior_turn": bool(state.get("prior_turn")),
        "seconds_left": round(state["deadline"].remaining(), 1),
    }


# ---------------------------------------------------------------- nodes


def _handoff(state: GraphState, to_node: str, reason: RoutingReason, started: float,
             iteration: int = 0, counterfactual: Optional[RouteTarget] = None) -> HandoffRecord:
    return HandoffRecord(
        from_node=state.get("_node", "graph"), to_node=to_node, reason=reason,
        elapsed_ms=int((time.monotonic() - started) * 1000),
        correlation_id=state.get("correlation_id", "-"), iteration=iteration,
        counterfactual=counterfactual,
    )


async def supervisor(state: GraphState, deps: Deps) -> Dict[str, Any]:
    """Decide the next node. The graph executes it; the model never runs anything itself."""
    started = time.monotonic()
    baseline = deterministic_next(state)
    decision = baseline                      # the fallback is the rule, so a model failure cannot stall the graph

    # A model call is only worth making where a rule genuinely cannot decide: a follow-up question, where the
    # choice is answer-from-state versus re-retrieve. Everywhere else the null checks already settle it.
    if state.get("prior_turn") and state.get("question") and deps.llm is not None:
        try:
            decision = await _ask_supervisor(deps, state) or baseline
        except Exception as e:
            log.warning("supervisor_fallback", extra={"error": type(e).__name__})

    # The divergence lives on the handoff record itself, which is logged, traced and returned in the API
    # response — so the rate is derivable without a separate counter to keep in sync.
    hop = _handoff(state, decision.next.value, decision.reason, started,
                   counterfactual=baseline.next if baseline.next != decision.next else None)
    return {"handoffs": [*state.get("handoffs", []), hop], "_route": decision.next.value,
            "outcome_reason": decision.reason}


async def _ask_supervisor(deps: Deps, state: GraphState) -> Optional[RoutingDecision]:
    """One small structured call over the state's SHAPE. Returns None if anything about it is unusable."""
    from anthropic import transform_schema

    resp = await deps.llm.messages.create(
        model=deps.settings.anthropic_model, max_tokens=200,
        timeout=max(0.1, state["deadline"].remaining() - NODE_MARGIN_S),
        messages=[{"role": "user", "content":
                   "Decide the next step for a clinical co-pilot answering a follow-up question.\n"
                   f"State: {state_shape(state)}\n"
                   "Choose `retrieve` only if the held evidence cannot support the new question."}],
        output_config={"type": "json_schema", "schema": transform_schema(RoutingDecision)},
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    return RoutingDecision.model_validate_json(text) if text else None


async def intake_extractor(state: GraphState, deps: Deps) -> Dict[str, Any]:
    """Read the document, then locate what was read. Loops only while looping can still change the answer."""
    started, pages, doc = time.monotonic(), state.get("pages"), state.get("document")
    if pages is None or doc is None:
        return {"handoffs": [*state.get("handoffs", []),
                             _handoff(state, "supervisor", RoutingReason.no_document_extracted, started)]}

    extracted, reason, iteration = None, RoutingReason.extraction_exhausted, 0
    for iteration in range(1, MAX_EXTRACT_ITERATIONS + 1):
        if not state["deadline"].has(NODE_MARGIN_S):
            reason = RoutingReason.deadline_expired
            break
        seen, meta = await extract_mod.read_document(deps.llm, deps.settings, doc.doc_type,
                                                     pages.images, state["deadline"])
        if seen is None:
            reason = (RoutingReason.deadline_expired if meta.reason in ("deadline", "timeout")
                      else RoutingReason.extraction_exhausted)
            continue
        extracted = extract_mod.assemble(seen, doc, pages)
        located, total = extract_mod.located_ratio(extracted)
        # DETERMINISTIC termination: the schema validated (assemble would have raised otherwise) and every field
        # is either located or explicitly unlocated. There is nothing a further pass could improve.
        if total == 0 or located == total:
            reason = RoutingReason.ready_to_answer
            break
        reason = RoutingReason.ready_to_answer      # partial location is an answer, not a retry condition
        break

    return {"extracted": extracted, "outcome_reason": reason,
            "handoffs": [*state.get("handoffs", []),
                         _handoff(state, "supervisor", reason, started, iteration=iteration)]}


async def evidence_retriever(state: GraphState, deps: Deps) -> Dict[str, Any]:
    """Find guideline evidence. Reformulates once if nothing clears the floor, then stops."""
    started, question = time.monotonic(), state.get("question") or ""
    evidence: List[EvidenceChunk] = []
    reason, iteration = RoutingReason.evidence_below_floor, 0

    queries = [question]
    if state.get("extracted") is not None:
        # One reformulation, built from what the document actually said — not a model-written query.
        titles = [c.quote_or_value for c in extract_mod._citations(state["extracted"])][:3]
        if titles:
            queries.append(" ".join([question, *titles]))

    for iteration, q in enumerate(queries[:MAX_RETRIEVE_ITERATIONS], start=1):
        if not state["deadline"].has(NODE_MARGIN_S):
            reason = RoutingReason.deadline_expired
            break
        try:
            evidence = deps.retriever.search(q) if deps.retriever else []
        except RetrievalUnavailable as e:
            log.warning("retrieval_unavailable", extra={"reason": str(e)})
            reason = RoutingReason.retrieval_exhausted
            break
        if evidence:                                  # DETERMINISTIC: chunks above the floor, or nothing
            reason = RoutingReason.ready_to_answer
            break

    return {"evidence": evidence, "outcome_reason": reason,
            "handoffs": [*state.get("handoffs", []),
                         _handoff(state, "supervisor", reason, started, iteration=iteration)]}


# ---------------------------------------------------------------- assembly


def route(state: GraphState) -> str:
    return state.get("_route", RouteTarget.answer.value)


def build_graph(deps: Deps, answer_node: Callable[[GraphState], Any]):
    """Wire the four nodes. Kept in one function so the shape is readable in one screen."""
    from langgraph.graph import END, StateGraph

    g = StateGraph(GraphState)
    g.add_node("supervisor", lambda s: supervisor(s, deps))
    g.add_node("extract", lambda s: intake_extractor(s, deps))
    g.add_node("retrieve", lambda s: evidence_retriever(s, deps))
    g.add_node("answer", answer_node)
    g.add_node("refuse", lambda s: {"outcome_reason": RoutingReason.out_of_scope})

    g.set_entry_point("supervisor")
    g.add_conditional_edges("supervisor", route,
                            {"extract": "extract", "retrieve": "retrieve",
                             "answer": "answer", "refuse": "refuse"})
    # Workers always hand back to the supervisor: it decides what is next, they never decide for it.
    g.add_edge("extract", "supervisor")
    g.add_edge("retrieve", "supervisor")
    g.add_edge("answer", END)
    g.add_edge("refuse", END)
    return g.compile()
