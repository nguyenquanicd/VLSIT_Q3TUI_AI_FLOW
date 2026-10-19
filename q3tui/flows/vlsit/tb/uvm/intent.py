"""The intent stage: the LLM's decisions about the draft contract, applied and checked by code (rules DUT-DV-006..009, 016, 017).

The model returns `DvIntent`. Code merges it into the code-made draft (it never edits expected behavior from anywhere else), derives the
requirement back-links, keeps IDs permanent, and lists every problem so a fix round can repair them."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from q3tui.flows.vlsit.tb.uvm.contract import (
    CoverageGoal, InterfaceSpec, Objective, OpenIssue, ScoreboardRule, TestSpec, VerificationContract, validate_contract)
from q3tui.steps.common import Question

ID_FORMATS = {"objectives": r"VO-\d{3}", "scoreboard_rules": r"SBR-\d{3}", "coverage": r"COV-\d{3}", "tests": r"TC-\d{3}", "open_issues": r"OI-\d{3}"}


class ReqDecision(BaseModel):
    req_id: str
    observability: Literal["PORT", "ELAB_CONFIG", "INTERNAL"]
    observability_reason: str = Field("", description="One short phrase why (required for INTERNAL)")
    origin: Literal["NORMATIVE", "ASSUMED"] | None = Field(None, description="Only for a requirement flagged ASSUMED_CANDIDATE: your decision")
    status: Literal["planned", "open", "blocked", "not_observable", "excluded"]
    status_reason: str = Field("", description="Required unless status is planned")


class DvIntent(BaseModel):
    decisions: list[ReqDecision]
    interfaces: list[InterfaceSpec] = Field(default_factory=list)
    objectives: list[Objective] = Field(default_factory=list)
    scoreboard_rules: list[ScoreboardRule] = Field(default_factory=list)
    coverage: list[CoverageGoal] = Field(default_factory=list)
    tests: list[TestSpec] = Field(default_factory=list)
    open_issues: list[OpenIssue] = Field(default_factory=list)
    summary: str = ""
    questions: list[Question] = Field(default_factory=list, description="Only questions about the specification itself (spec_gap / req_gap)")


def _id_key(item) -> str:
    return getattr(item, "tc_id", None) or item.id


def apply_intent(draft: VerificationContract, intent: DvIntent, previous: VerificationContract | None = None,
                 answered: set[str] | None = None) -> tuple[VerificationContract, list[str]]:
    """The draft completed by the intent, and the problems of the result (empty: the contract can be frozen)."""
    problems: list[str] = []
    reqs = {r.req_id: r for r in draft.requirements}
    for d in intent.decisions:
        r = reqs.get(d.req_id)
        if r is None:
            problems.append(f"decision for unknown requirement {d.req_id}")
            continue
        update = {"observability": d.observability, "observability_reason": d.observability_reason, "status": d.status, "status_reason": d.status_reason}
        if r.origin_class == "ASSUMED_CANDIDATE" and d.origin:
            update["origin_class"] = d.origin
        reqs[d.req_id] = r.model_copy(update=update)
    missing = [i for i in reqs if i not in {d.req_id for d in intent.decisions}]
    if missing:
        problems.append("no decision for: " + ", ".join(missing[:12]) + (" …" if len(missing) > 12 else ""))
    for key, fmt in ID_FORMATS.items():
        for item in getattr(intent, key):
            if not re.fullmatch(fmt, _id_key(item)):
                problems.append(f"{key}: ID '{_id_key(item)}' must look like {fmt}")
    if previous is not None:  # IDs are permanent (DUT-DV-017)
        for key in ID_FORMATS:
            gone = {_id_key(i) for i in getattr(previous, key)} - {_id_key(i) for i in getattr(intent, key)}
            if gone:
                problems.append(f"{key}: permanent ID(s) dropped: {', '.join(sorted(gone))} (keep them; a removed item is withdrawn, not deleted)")

    # back-links from the items to the requirements
    links = {r: {"objective_ids": [], "rule_ids": [], "test_ids": [], "coverage_ids": []} for r in reqs}
    def link(field: str, item_id: str, ids: list[str]) -> None:
        for r in ids:
            if r in links and item_id not in links[r][field]:
                links[r][field].append(item_id)
    for o in intent.objectives:
        link("objective_ids", o.id, o.requirement_ids)
    for s in intent.scoreboard_rules:
        link("rule_ids", s.id, s.requirement_ids)
    for t in intent.tests:
        link("test_ids", t.tc_id, t.requirement_ids)
    for g in intent.coverage:
        link("coverage_ids", g.id, g.requirement_ids)
    for o in intent.objectives:  # an objective's tests and goals reach its requirements too
        for t in intent.tests:
            if o.id in t.objective_ids:
                link("test_ids", t.tc_id, o.requirement_ids)
        for g in intent.coverage:
            if o.id in g.objective_ids:
                link("coverage_ids", g.id, o.requirement_ids)
    # an open issue that existed keeps its question, source, default and requirements: the user reviewed exactly that text. The model may
    # only resolve it (DUT-DV-006, 017); a changed wording would make an earlier review say something the user never saw
    if previous is not None:
        old = {o.id: o for o in previous.open_issues}
        intent.open_issues = [o.model_copy(update={"kind": old[o.id].kind, "question": old[o.id].question, "source": old[o.id].source,
                                                   "default_assumption": old[o.id].default_assumption, "requirement_ids": old[o.id].requirement_ids,
                                                   "resolved": o.resolved or old[o.id].resolved, "resolution": o.resolution or old[o.id].resolution})
                              if o.id in old else o for o in intent.open_issues]
    out_reqs = [reqs[r.req_id].model_copy(update=links[r.req_id]) for r in draft.requirements]
    ic = draft.interface_contract.model_copy(update={"interfaces": intent.interfaces})
    c = draft.model_copy(update={
        "requirements": out_reqs, "interface_contract": ic, "objectives": intent.objectives, "scoreboard_rules": intent.scoreboard_rules,
        "coverage": intent.coverage, "tests": intent.tests, "open_issues": intent.open_issues})
    problems += validate_contract(c, final=True)
    for o in c.open_issues:  # a question the user answered must be applied, never left open (DUT-DV-006)
        if o.id in (answered or set()) and not (o.resolved and o.resolution.strip()):
            problems.append(f"{o.id}: the user answered it: apply the answer, set resolved=true and state the resolution")
    by_req = {r.req_id: r for r in c.requirements}
    for r in c.requirements:  # a candidate decided NORMATIVE needs its reason and may not sit in an unresolved open issue (DUT-DV-016)
        if r.assumption_evidence and r.origin_class == "NORMATIVE":
            if not r.status_reason.strip():
                problems.append(f"{r.req_id}: decided NORMATIVE although it may restate an assumption ({', '.join(r.assumption_evidence[:2])}): give the reason in status_reason")
            if any(r.req_id in o.requirement_ids and not o.resolved for o in c.open_issues):
                problems.append(f"{r.req_id}: it is NORMATIVE but also in an unresolved open issue: decide one (NORMATIVE, or ASSUMED and open)")
    for o in c.open_issues:
        if not o.question.strip():
            problems.append(f"{o.id}: an open issue needs its question")
    for r in c.requirements:  # an open or blocked requirement is an open issue the user can see
        if r.status in ("open", "blocked") and not any(r.req_id in o.requirement_ids and not o.resolved for o in c.open_issues):
            problems.append(f"{r.req_id}: status {r.status} needs an open issue (OI-…) that carries the question and the default")
    return c, problems


def question_id(issue_id: str) -> str:
    """Stable question ID of an open issue (OI IDs are permanent): answers stay attached to the right issue across runs."""
    return f"Q-TB-{issue_id}"


def issue_questions(c: VerificationContract) -> list[Question]:
    """The open issues as the step's questions (blocking only for BLOCKED): DUT-DV-006 never lets a guess pass as a decision."""
    out = []
    for o in c.open_issues:
        if o.resolved:
            continue
        out.append(Question(id=question_id(o.id), question=f"{o.question} [{o.id}; {', '.join(o.requirement_ids)}" + (f"; {o.source}" if o.source else "") + "]",
                            blocking=o.kind == "BLOCKED", default_assumption=o.default_assumption, kind="spec_gap"))
    return out
