"""The golden manifest and the verification contract of the `uvm_vip` backend (rules DUT-DV-001..009, 016..018).

`golden_manifest.json` pins the behavioral sources (path, SHA-256, approval). `verification_contract.json` is the machine-readable
expression of the approved specification that the reference model, tests, assertions and coverage plan are built from. The
draft is made by code from the approved artifacts (structured_spec.json, final_config.json, the spec documents); the intent stage
(LLM) completes the semantic columns; code validates it and freezes it by hash before any RTL integration (DUT-DV-026).
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from q3tui.flows.vlsit.tb.uvm import spec_facts as sf

SCHEMA_VERSION = 1
Observability = Literal["PORT", "ELAB_CONFIG", "INTERNAL", "UNKNOWN"]
Origin = Literal["NORMATIVE", "CONSTRAINT", "ASSUMED_CANDIDATE", "ASSUMED"]
ReqStatus = Literal["draft", "planned", "open", "blocked", "not_observable", "excluded", "withdrawn"]
# a requirement that is not checked by any test or rule must say why with one of these (no silent drop)
RESOLVED = ("open", "blocked", "not_observable", "excluded", "withdrawn")

# fields of the approved structured spec that describe the implementation's structure (design-flow output), never behavior
EXCLUDED_SPEC_FIELDS = {
    "requirements[].rtl_modules": "names RTL modules that implement the requirement (implementation structure)",
    "modules": "module hierarchy of the implementation",
    "module_order": "implementation build order",
}


class _S(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceRef(_S):
    kind: Literal["design_spec", "structured_spec", "final_config", "standard"]
    path: str
    sha256: str
    revision: str = ""
    approved: bool = False
    approved_at: str | None = None


class ExcludedInput(_S):
    where: str
    reason: str


class GoldenManifest(_S):
    schema_version: int = SCHEMA_VERSION
    ip_name: str
    sources: list[SourceRef]
    excluded: list[ExcludedInput] = Field(default_factory=list)


class PortSpec(_S):
    name: str
    direction: str
    width: str
    reset_value: str = ""
    description: str = ""
    section: str = ""
    source: str = ""  # spec path:line
    assumed: bool = False  # the specification marks (part of) this row as an assumption


class ParameterSpec(_S):
    name: str
    value: str
    default: str
    legal: str = ""
    type: str = ""


class ConstraintSpec(_S):
    id: str
    rule: str
    description: str = ""
    assumed: bool = False


class InterfaceSpec(_S):
    id: str
    kind: str = Field(description="axi4_slave | sram | clock_reset | other")
    protocol: str = ""
    role: str = Field("", description="role of the DUT on this interface (slave, master, memory master, ...)")
    ports: list[str] = Field(default_factory=list)
    stimulus_agent: str = Field("", description="who drives the DUT-facing side: vip_master, sram_model, tb_clock_reset, none")
    requirement_ids: list[str] = Field(default_factory=list)


class InterfaceContract(_S):
    top_module: str
    clock: str = ""
    reset: str = ""
    reset_active_low: bool | None = None
    ports: list[PortSpec] = Field(default_factory=list)
    parameters: list[ParameterSpec] = Field(default_factory=list)
    constraints: list[ConstraintSpec] = Field(default_factory=list)
    interfaces: list[InterfaceSpec] = Field(default_factory=list)


class Requirement(_S):
    req_id: str
    text: str
    category: str = ""
    strength: Literal["MUST", "SHOULD", "MAY", "UNSTATED"] = "UNSTATED"
    source_section: str = ""
    feature_id: str = ""
    parameters_affected: list[str] = Field(default_factory=list)
    ambiguity_score: float = 0.0
    check_hint: str | None = Field(None, description="sva_hint of the approved structured spec (informational)")
    origin_class: Origin = "NORMATIVE"
    assumption_evidence: list[str] = Field(default_factory=list, description="spec path:line of an assumption this may restate")
    observability_hint: Observability = "UNKNOWN"  # by code, from the text
    observability: Observability = "UNKNOWN"       # confirmed by the intent stage
    observability_reason: str = ""
    status: ReqStatus = "draft"
    status_reason: str = ""
    objective_ids: list[str] = Field(default_factory=list)
    rule_ids: list[str] = Field(default_factory=list)
    test_ids: list[str] = Field(default_factory=list)
    coverage_ids: list[str] = Field(default_factory=list)


class Objective(_S):
    id: str
    title: str
    requirement_ids: list[str]
    status: Literal["approved", "proposed", "withdrawn"] = "proposed"


class ScoreboardRule(_S):
    id: str
    description: str
    requirement_ids: list[str]
    source: str = Field(description="specification reference: section and the sentence it comes from (DUT-DV-008)")
    in_smoke: bool = False
    implementation: Literal["todo", "implemented"] = "todo"


class CoverageGoal(_S):
    id: str
    description: str
    requirement_ids: list[str] = Field(default_factory=list)
    objective_ids: list[str] = Field(default_factory=list)


class TestSpec(_S):
    tc_id: str
    name: str
    description: str = ""
    kind: Literal["smoke", "directed", "negative", "random"] = "directed"
    milestone: Literal["v0_1", "later"] = "later"
    requirement_ids: list[str] = Field(default_factory=list)
    objective_ids: list[str] = Field(default_factory=list)
    rule_ids: list[str] = Field(default_factory=list)
    conditional_param: str | None = None


class OpenIssue(_S):
    id: str
    kind: Literal["OPEN", "BLOCKED"]
    requirement_ids: list[str] = Field(default_factory=list)
    question: str
    source: str = ""
    default_assumption: str = ""
    resolved: bool = Field(False, description="True once the user answered the question; the answer is then applied in the contract")
    resolution: str = Field("", description="What the user's answer decided (when resolved)")


class Metadata(_S):
    ip_name: str
    top_module: str
    spec_revision: str = ""
    status: Literal["draft", "frozen"] = "draft"
    contract_hash: str = ""
    frozen_at: str | None = None


class VerificationContract(_S):
    schema_version: int = SCHEMA_VERSION
    metadata: Metadata
    interface_contract: InterfaceContract
    requirements: list[Requirement]
    objectives: list[Objective] = Field(default_factory=list)
    scoreboard_rules: list[ScoreboardRule] = Field(default_factory=list)
    coverage: list[CoverageGoal] = Field(default_factory=list)
    tests: list[TestSpec] = Field(default_factory=list)
    open_issues: list[OpenIssue] = Field(default_factory=list)


# -- building the draft (code) -------------------------------------------------------------------------------------------

def _sha(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def build_manifest(root: Path, spec_docs: list[Path], spec_json: Path, config_json: Path, standards: list[Path],
                   structured: dict, config: dict) -> GoldenManifest:
    def rel(p: Path) -> str:
        try:
            return str(p.resolve().relative_to(root.resolve()))
        except ValueError:
            return str(p)

    md = structured.get("metadata") or {}
    cm = config.get("metadata") or {}
    rev = str(md.get("spec_revision", ""))
    # (Gate 1 approves the requirements extracted from these documents: the documents count as approved with it)
    sources = [SourceRef(kind="design_spec", path=rel(p), sha256=_sha(p), revision=rev, approved=md.get("gate_status") == "approved",
                         approved_at=md.get("approved_at")) for p in spec_docs]
    sources.append(SourceRef(kind="structured_spec", path=rel(spec_json), sha256=_sha(spec_json), revision=rev,
                             approved=md.get("gate_status") == "approved", approved_at=md.get("approved_at")))
    sources.append(SourceRef(kind="final_config", path=rel(config_json), sha256=_sha(config_json), revision=str(cm.get("spec_revision", "")),
                             approved=cm.get("gate_status") == "approved", approved_at=cm.get("approved_at")))
    sources += [SourceRef(kind="standard", path=rel(p), sha256=_sha(p)) for p in standards]
    excluded = [ExcludedInput(where=f"schemas/structured_spec.json: {k}", reason=v) for k, v in EXCLUDED_SPEC_FIELDS.items()]
    for p in spec_docs:
        for h in sf.excluded_sections(p.read_text(errors="replace")):
            excluded.append(ExcludedInput(where=f"{rel(p)}: section '{h}'", reason="design-flow result derived from the RTL or its testbench (DUT-DV-019)"))
    return GoldenManifest(ip_name=str(md.get("ip_name", "")), sources=sources, excluded=excluded)


def strength_of(text: str) -> str:
    t = text.lower()
    if re.search(r"\bshall not\b|\bmust not\b", t) or re.search(r"\b(shall|must)\b", t):
        return "MUST"
    if re.search(r"\bshould\b", t):
        return "SHOULD"
    if re.search(r"\bmay\b", t):
        return "MAY"
    return "UNSTATED"


def draft_contract(structured: dict, config: dict, spec_texts: dict[str, str]) -> VerificationContract:
    """The contract as far as code can take it: sources, interface, parameters, requirements with their flags."""
    md = structured.get("metadata") or {}
    notes: list[tuple[str, sf.Assumption]] = []
    port_rows: list[tuple[str, sf.Port]] = []
    for path, text in spec_texts.items():
        notes += [(path, a) for a in sf.assumptions(text)]
        port_rows += [(path, p) for p in sf.ports(text)]
    all_notes = [a for _, a in notes]
    where = {id(a): f"{path}:{a.line}" for path, a in notes}

    ports: list[PortSpec] = []
    seen: set[str] = set()
    for path, p in port_rows:
        if p.name in seen:
            continue
        seen.add(p.name)
        row_assumed = any(a.kind == "cell" and a.line == p.line for a in all_notes)
        ports.append(PortSpec(name=p.name, direction=p.direction, width=p.width, reset_value=p.reset_value, description=p.description,
                              section=p.section, source=f"{path}:{p.line}", assumed=row_assumed))
    known = {p.name for p in ports}

    params = []
    cparams = (config.get("parameters") or {})
    for sp in structured.get("parameters") or []:
        cv = cparams.get(sp["name"]) or {}
        params.append(ParameterSpec(name=sp["name"], value=str(cv.get("value", sp.get("default", ""))), default=str(sp.get("default", "")),
                                    legal=str(sp.get("valid_range", "")), type=str(sp.get("type", ""))))
    constraints = []
    for c in structured.get("constraints") or []:
        assumed = any(a.kind != "cell" and re.match(rf"\s*{re.escape(c['id'])}\b", a.text) for a in all_notes)
        constraints.append(ConstraintSpec(id=c["id"], rule=c["rule"], description=c.get("description", ""), assumed=assumed or "assumption" in c.get("description", "").lower()))

    clock = next((p.name for p in ports if "clk" in p.name), "")
    reset = next((p.name for p in ports if "rst" in p.name or "reset" in p.name), "")
    ic = InterfaceContract(top_module=str(md.get("top_module", "")), clock=clock, reset=reset,
                           reset_active_low=(reset.endswith("_n") if reset else None), ports=ports, parameters=params, constraints=constraints)

    reqs = []
    for r in structured.get("requirements") or []:
        text = r["text"]
        cands = sf.assumed_candidates(text, all_notes)
        internal = sf.internal_names(text, known)
        is_constraint = r.get("category") == "constraint"
        origin = "ASSUMED_CANDIDATE" if cands else ("CONSTRAINT" if is_constraint else "NORMATIVE")
        hint = "ELAB_CONFIG" if is_constraint else ("INTERNAL" if internal and not (set(sf.idents(text)) & known) else "PORT" if set(sf.idents(text)) & known else "UNKNOWN")
        reqs.append(Requirement(
            req_id=r["req_id"], text=text, category=r.get("category", ""), strength=strength_of(text),
            source_section=r.get("source_section", ""), feature_id=r.get("feature_id", ""), parameters_affected=list(r.get("parameters_affected") or []),
            ambiguity_score=float(r.get("ambiguity_score", 0.0)), check_hint=r.get("sva_hint"), origin_class=origin,
            assumption_evidence=[where[id(a)] for a in cands], observability_hint=hint))
    return VerificationContract(
        metadata=Metadata(ip_name=str(md.get("ip_name", "")), top_module=str(md.get("top_module", "")), spec_revision=str(md.get("spec_revision", ""))),
        interface_contract=ic, requirements=reqs)


# -- validation and freezing ----------------------------------------------------------------------------------------------

def validate_contract(c: VerificationContract, final: bool = False) -> list[str]:
    """Problems of a contract: duplicate or dangling IDs, items without a specification trace, requirements dropped silently.
    `final`: also every requirement must be settled (planned or resolved with a reason)."""
    p: list[str] = []
    req = {r.req_id for r in c.requirements}
    if len(req) != len(c.requirements):
        p.append("duplicate requirement IDs")
    groups = {"objective": (c.objectives, "id"), "scoreboard rule": (c.scoreboard_rules, "id"), "coverage goal": (c.coverage, "id"),
              "test": (c.tests, "tc_id"), "open issue": (c.open_issues, "id")}
    ids: dict[str, set[str]] = {}
    for label, (items, key) in groups.items():
        vals = [getattr(i, key) for i in items]
        if len(set(vals)) != len(vals):
            p.append(f"duplicate {label} IDs")
        ids[label] = set(vals)
    vo, sbr, cov, tc = ids["objective"], ids["scoreboard rule"], ids["coverage goal"], ids["test"]
    def dangling(label: str, owner: str, refs: list[str], known: set[str]) -> None:
        for r in refs:
            if r not in known:
                p.append(f"{owner}: dangling {label} ID {r}")
    for o in c.objectives:
        if not o.requirement_ids:
            p.append(f"{o.id}: an objective needs at least one requirement")
        dangling("requirement", o.id, o.requirement_ids, req)
    for r in c.scoreboard_rules:
        if not r.requirement_ids or not r.source.strip():
            p.append(f"{r.id}: a scoreboard rule needs requirement IDs and a specification source (DUT-DV-008)")
        dangling("requirement", r.id, r.requirement_ids, req)
    for g in c.coverage:
        if not (g.requirement_ids or g.objective_ids):
            p.append(f"{g.id}: a coverage goal must trace to a requirement or objective (DUT-DV-009)")
        dangling("requirement", g.id, g.requirement_ids, req)
        dangling("objective", g.id, g.objective_ids, vo)
    for t in c.tests:
        if not (t.requirement_ids or t.objective_ids):
            p.append(f"{t.tc_id}: a test must trace to a requirement or objective (DUT-DV-007)")
        dangling("requirement", t.tc_id, t.requirement_ids, req)
        dangling("objective", t.tc_id, t.objective_ids, vo)
        dangling("scoreboard rule", t.tc_id, t.rule_ids, sbr)
    for i in c.open_issues:
        dangling("requirement", i.id, i.requirement_ids, req)
    for r in c.requirements:
        dangling("objective", r.req_id, r.objective_ids, vo)
        dangling("scoreboard rule", r.req_id, r.rule_ids, sbr)
        dangling("test", r.req_id, r.test_ids, tc)
        dangling("coverage goal", r.req_id, r.coverage_ids, cov)
        if r.status in RESOLVED and not r.status_reason.strip():
            p.append(f"{r.req_id}: status {r.status} needs a reason (DUT-DV-006, 016)")
        if final:
            traced = r.objective_ids or r.rule_ids or r.test_ids or r.coverage_ids
            if r.status == "draft" or (r.status == "planned" and not traced):
                p.append(f"{r.req_id}: not settled (neither planned with a trace nor resolved with a reason)")
            if r.origin_class == "ASSUMED_CANDIDATE":
                p.append(f"{r.req_id}: may restate a specification assumption ({', '.join(r.assumption_evidence)}): "
                         "decide origin NORMATIVE (with the reason) or ASSUMED (then open or blocked) (DUT-DV-006, 016)")
            if r.origin_class == "ASSUMED" and r.status not in ("open", "blocked", "excluded", "withdrawn"):
                p.append(f"{r.req_id}: an assumed behavior is not golden: its status must be open or blocked, not {r.status} (DUT-DV-006)")
            if r.observability == "UNKNOWN":
                p.append(f"{r.req_id}: observability is not decided (PORT, ELAB_CONFIG or INTERNAL)")
            if r.observability == "INTERNAL" and r.status in ("planned", "draft"):
                p.append(f"{r.req_id}: internal structure cannot be checked at the DUT boundary: not_observable (DUT-DV-016), not {r.status}")
    if not c.tests:
        p.append("no test at all") if final else None
    if final and not any(t.kind == "smoke" and t.milestone == "v0_1" for t in c.tests):
        p.append("no deterministic smoke test (DUT-DV-015)")
    for t in c.tests:
        if t.kind == "smoke" and not t.rule_ids and final:
            p.append(f"{t.tc_id}: the smoke test names no scoreboard rule")
    return p


def contract_hash(c: VerificationContract) -> str:
    data = c.model_dump(mode="json")
    data["metadata"] = {k: v for k, v in data["metadata"].items() if k not in ("status", "contract_hash", "frozen_at")}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def freeze(c: VerificationContract, now: str) -> VerificationContract:
    """Mark the contract frozen: the hash of its content is recorded; any later change shows (DUT-DV-026)."""
    h = contract_hash(c)
    return c.model_copy(update={"metadata": c.metadata.model_copy(update={"status": "frozen", "contract_hash": h, "frozen_at": now})})


def frozen_ok(c: VerificationContract) -> bool:
    return c.metadata.status == "frozen" and c.metadata.contract_hash == contract_hash(c)
