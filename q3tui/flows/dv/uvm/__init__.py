"""The `dv` flow's UVM testbench (docs/spec/dut-dv.md): a UVM DUT testbench from the approved specification and an existing reusable protocol VIP.

The flow has three steps of its own after the spec flow's: `testplan` (the verification contract: objectives, scoreboard rules, coverage, the tests, the open
issues you answer; `run_testplan`), `tb` (the architecture, the VIP binding, the scaffold, the agent's regions, then the RTL as the DUT: conformance, wrapper,
compile; `run_testbench`) and `simulate` (the smoke test and the directed tests, judged from the logs; `run_simulate`). The options are the `tb` step's
(`pipeline.step_options.tb.uvm_vip`), read by all three (`tb_options`), and keep three input classes apart (rules DUT-DV-001..004, 026..027):

| class | where it comes from | may define expected behavior |
|---|---|---|
| behavioral | project artifacts: spec documents, structured_spec.json, final_config.json, `standards` | yes |
| vip | `uvm_vip.vip` (location, revision, kind, interface configuration) | no: describes the reusable component |
| integration | `uvm_vip.integration` (RTL location, top, include dirs, defines, simulator) | never |
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

# input class of each option group (machine-checkable: tests and the validator read this table)
INPUT_CLASSES = {"standards": "behavioral", "vip": "vip", "integration": "integration"}


class OptionsError(ValueError):
    """The tb options of a `uvm_vip` run are not valid; the message names the option."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VipInputs(_Strict):
    """The existing reusable VIP: read-only input, pinned by location and revision (DUT-DV-010, 020)."""

    path: str = Field(description="Directory of the VIP (outside or inside the project); never modified by the flow")
    kind: str = Field("custom", description="Family hint for the probe: axi4_avip | svt | custom (the probe still reads the source)")
    revision: str | None = Field(None, description="git commit / tag / version of the VIP; recorded in vip_integration.json")
    config: dict[str, str | int] = Field(default_factory=dict, description="VIP configuration the DUT interface needs (widths, mode)")
    env: str | None = Field(None, description="Name of the environment variable that holds the VIP location (e.g. AXI_VIP_HOME): the generated file lists then "
                                              "use $NAME, so they stay portable (the configuration loader expands $VARs in `path` before the step sees it)")


class IntegrationInputs(_Strict):
    """How the released RTL is built and bound. Used only after the verification contract is frozen (DUT-DV-026)."""

    rtl_dir: str = Field("src/rtl", description="RTL location relative to the project root")
    top: str | None = Field(None, description="DUT top module; null: from the specification's metadata, confirmed by the integration")
    incdirs: list[str] = Field(default_factory=list)
    defines: dict[str, str | None] = Field(default_factory=dict)
    simulator: Literal["vcs"] = "vcs"
    smoke_test: str = "dut_smoke_test"
    # approved name mappings (DUT-DV-030): specification name -> RTL name. Never inferred: a rename is reported until a mapping is approved here.
    param_map: dict[str, str] = Field(default_factory=dict)
    port_map: dict[str, str] = Field(default_factory=dict)


class SemanticOptions(_Strict):
    """Which regions the agent implements (milestone M5 and later)."""

    scope: Literal["smoke", "all"] = Field("smoke", description="smoke: the smoke path only; all: also the other scoreboard rules and the directed tests "
                                                                "(coverage and assertions stay stubs)")


class UvmVipOptions(_Strict):
    standards: list[str] = Field(default_factory=list, description="Approved standard documents (behavioral); paths relative to the project")
    vip: VipInputs | None = Field(None, description="The existing protocol VIP, or none: a simple IP has no protocol interface to drive with one. The `tb` step needs it only "
                                                    "for an interface whose stimulus agent is `vip_master`")
    integration: IntegrationInputs = Field(default_factory=IntegrationInputs)
    semantic: SemanticOptions = Field(default_factory=SemanticOptions)


# options a user sets in the Settings dialog (Settings → tb) or the assistant sit flat on the tb step; they are the nested `uvm_vip` group's leaves
FLAT_OPTIONS = {"vip_path": ("vip", "path"), "vip_kind": ("vip", "kind"), "vip_revision": ("vip", "revision"), "vip_env": ("vip", "env"),
                "rtl_dir": ("integration", "rtl_dir"), "top": ("integration", "top"), "smoke_test": ("integration", "smoke_test"), "scope": ("semantic", "scope")}


def merged_options(options: dict | None) -> dict | None:
    """The `uvm_vip` group with the flat options laid over it (a value set in the Settings dialog wins over q3tui.yaml's group); None when neither is given."""
    options = options or {}
    group = options.get("uvm_vip")
    flat = {k: v for k, v in options.items() if k in FLAT_OPTIONS and v not in (None, "")}
    if not isinstance(group, dict) and not flat:
        return None
    out = json.loads(json.dumps(group)) if isinstance(group, dict) else {}
    for key, value in flat.items():
        section, leaf = FLAT_OPTIONS[key]
        out.setdefault(section, {})[leaf] = value
    return out


def testbench_kind(options: dict | None) -> str:
    """`plain` or `uvm`: the kind of testbench the three steps of the dv flow build, from the `testbench` option of the tb step (Settings → tb). `auto` (the default): `uvm`
    when a VIP location is set (a VIP means a UVM environment), `plain` otherwise (any IP: the UVM generator covers AXI-style memory-mapped IPs only so far)."""
    value = (options or {}).get("testbench") or "auto"
    if value in ("plain", "uvm"):
        return value
    if value != "auto":
        raise OptionsError(f"tb option testbench: '{value}' is not one of auto, plain, uvm")
    raw = merged_options(options) or {}
    return "uvm" if (raw.get("vip") or {}).get("path") else "plain"


def parse_options(options: dict | None) -> UvmVipOptions:
    """The validated `uvm_vip` options (the group `options['uvm_vip']` and the flat options of the Settings dialog; none given: the defaults, without a VIP); a clear error
    names what is unknown. A `vip` without a path is the same as none."""
    raw = merged_options(options) or {}
    if not (raw.get("vip") or {}).get("path"):
        raw.pop("vip", None)
    try:
        return UvmVipOptions.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first["loc"])
        raise OptionsError(f"tb option uvm_vip.{where}: {first['msg']}") from exc


def resolve_path(value: str, project_root: Path) -> Path:
    p = Path(os.path.expandvars(value)).expanduser()
    return p if p.is_absolute() else (project_root / p).resolve()


def _digest(data: object) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:16]


def behavioral_fingerprint(options: dict | None) -> str:
    """Hash of what may shape expected behavior and the TB architecture: the standards and the VIP
    inputs. Integration inputs are left out on purpose: changing the RTL location, defines or include directories
    never makes the behavioral phase stale (DUT-DV-026)."""
    o = parse_options(options)
    return _digest({"backend": "uvm_vip", "standards": o.standards, "vip": o.vip.model_dump() if o.vip else None})


def integration_fingerprint(options: dict | None) -> str:
    """Hash of the integration inputs only (compile and run configuration)."""
    return _digest(parse_options(options).integration.model_dump())


def input_problem(options: dict | None, project_root: Path) -> str | None:
    """Why the dv flow's UVM steps cannot start (None: the options are usable)."""
    try:
        o = parse_options(options)
    except OptionsError as exc:
        return str(exc)
    if o.vip and not resolve_path(o.vip.path, project_root).is_dir():
        return f"the VIP was not found: uvm_vip.vip.path = {o.vip.path} (the VIP is an existing component; the flow never generates it)"
    return None


TB_EVIDENCE = "dv_tb_evidence.json"  # the compiles of the `tb` step: against the interface shell (a syntax / integration check, never a DUT result) and with the RTL
EVIDENCE = "dv_evidence.json"  # the simulations of the `simulate` step: only what ran

# what each step writes (schemas/, reports/, src/tb/): hashed, reset and checked for edits by the engine
TESTPLAN_SCHEMAS = ("golden_manifest.json", "verification_contract.json", "dv_review.json")
TESTPLAN_REPORTS = ("dv_review_report.md",)
TB_SCHEMAS = ("tb_architecture.json", "vip_integration.json", "dv_worklist.json", "dv_validation.json", "dut_integration.json", TB_EVIDENCE)
TB_REPORTS = ("tb_architecture.md", "dut_conformance_report.md")
SIM_SCHEMAS = (EVIDENCE, "dv_triage.json")
SIM_REPORTS = ("dv_test_results.md",)


def tb_options(engine) -> dict:
    """The options of the dv flow's UVM steps: the `tb` step's (`uvm_vip`), whichever step reads them."""
    tb = engine.by_name.get("tb")
    return tb.options if tb is not None else {}


class NotApproved(RuntimeError):
    pass


def require_approved(structured: dict, config: dict) -> None:
    """DUT-DV-001: only the approved specification artifacts are behavioral sources."""
    for label, art in (("structured_spec.json (Gate 1)", structured), ("final_config.json (Gate 2)", config)):
        if (art.get("metadata") or {}).get("gate_status") != "approved":
            raise NotApproved(f"{label} is not approved: the verification contract is built from approved sources only (DUT-DV-001)")


def _write_questions(step, ctx, questions: list) -> None:
    """The intent's questions in the step's question files (answered ones stay; same file format as the plain-SV step)."""
    from q3tui.core.project import read_json, write_json
    from q3tui.steps.common import Question, assign_question_ids, load_answers, render_questions

    engine = ctx.engine
    q_json, answers_path, q_md = step._q_paths(engine)
    prev = [Question.model_validate(q) for q in read_json(q_json, default=[]) or []]
    answers = load_answers(answers_path)
    kept = [q for q in prev if q.id in answers]  # answered questions stay on file
    fresh = [q for q in questions if q.question.strip().lower() not in {k.question.strip().lower() for k in kept}]
    stable = [q for q in fresh if q.id.startswith("Q-TB-OI-")]  # open issues: the ID comes from the permanent issue ID
    ided = stable + assign_question_ids([q for q in fresh if q not in stable], prev, "Q-TB-", taken={q.id for q in kept} | {q.id for q in stable})
    entries = [q.model_dump() for q in [*kept, *ided]]
    q_json.parent.mkdir(parents=True, exist_ok=True)
    write_json(q_json, entries)
    qs = [Question.model_validate(q) for q in entries]
    if qs:
        q_md.write_text(render_questions(qs, answers, f"Open questions — {step.title}", engine.state.accepted_defaults.get(step.name, {})))
    open_q = [q for q in qs if q.id not in answers]
    ctx.emit("questions", count=len(open_q), blocking=sum(q.blocking for q in open_q))


def refresh_review(step, engine, contract) -> None:
    """Rewrite schemas/dv_review.json and reports/dv_review_report.md from the contract, the answers and the accepted defaults."""
    from q3tui.flows.dv.uvm import review
    from q3tui.flows.spec.lib import artifacts

    lay = step.lay(engine)
    review.write_report(engine.project, step.name, contract, dict(engine.state.accepted_defaults.get(step.name, {})), lay.reports, artifacts.now())


def refresh_review_from_disk(step, engine) -> None:
    from q3tui.core.project import read_json
    from q3tui.flows.dv.uvm import contract as C
    from q3tui.flows.spec.lib import artifacts

    p = artifacts.path(engine.project, "verification_contract.json")
    if p.is_file():
        refresh_review(step, engine, C.VerificationContract.model_validate(read_json(p)))


def testplan_files(step, engine) -> list[Path]:
    """Files the `testplan` step has written: the golden manifest, the verification contract and the review of its open issues."""
    lay = step.lay(engine)
    return [p for p in [*(lay.schemas / n for n in TESTPLAN_SCHEMAS), *(lay.reports / n for n in TESTPLAN_REPORTS)] if p.is_file()]


def tb_files(step, engine) -> list[Path]:
    """Files the `tb` step has written: the architecture, the VIP binding, the worklist, the integration of the RTL and the testbench itself."""
    lay = step.lay(engine)
    names = [*(lay.schemas / n for n in TB_SCHEMAS), *(lay.reports / n for n in TB_REPORTS)]
    tb = [p for p in sorted(lay.tb.rglob("*")) if p.is_file() and (p.suffix in (".sv", ".svh", ".f") or p.name.endswith(".scaffold_new"))] if lay.tb.is_dir() else []
    return [p for p in [*names, *tb] if p.is_file()]


def simulate_files(step, engine) -> list[Path]:
    """Files the `simulate` step has written: the evidence of the runs and the table of the test results."""
    lay = step.lay(engine)
    return [p for p in [*(lay.schemas / n for n in SIM_SCHEMAS), *(lay.reports / n for n in SIM_REPORTS)] if p.is_file()]


async def contract_phase(step, ctx, opts: UvmVipOptions, raw: dict):
    """The `testplan` step: spec to verification contract, frozen by hash unless an issue is BLOCKED (no RTL involved); `raw` are the tb options (their
    behavioral part is in the fingerprint of what the contract was built from). Returns the contract."""
    from q3tui.core.project import sha256_json
    from q3tui.flows.dv.uvm import contract as C
    from q3tui.flows.dv.uvm import task as tasks
    from q3tui.flows.dv.uvm.intent import DvIntent, apply_intent, drop_repeats, issue_questions
    from q3tui.llm.runtime import Stage
    from q3tui.pipeline.base import StepFailed
    from q3tui.steps.common import load_answers, settled_block, split_feedback
    from q3tui.flows.spec.lib import artifacts

    engine, project = ctx.engine, ctx.project
    lay = step.lay(engine)
    structured, config = artifacts.read(project, "structured_spec.json"), artifacts.read(project, "final_config.json")
    if not structured or not config:
        raise StepFailed("schemas/structured_spec.json and schemas/final_config.json are needed (steps parse and config)")
    try:
        require_approved(structured, config)
    except NotApproved as exc:
        raise StepFailed(str(exc)) from exc

    docs = project.spec_documents()
    standards = [resolve_path(x, project.root) for x in opts.standards]
    manifest = C.build_manifest(project.root, docs, artifacts.path(project, "structured_spec.json"), artifacts.path(project, "final_config.json"),
                                standards, structured, config)
    spec_texts = {project.rel(p): p.read_text(errors="replace") for p in docs if p.suffix.lower() in (".md", ".txt")}
    draft = C.draft_contract(structured, config, spec_texts)
    meta = engine.state.step_meta.setdefault(step.name, {}).setdefault("uvm", {})
    c_path = artifacts.path(project, "verification_contract.json")
    from q3tui.core.project import read_json, sha256_file

    previous = None
    if c_path.is_file():
        try:
            previous = C.VerificationContract.model_validate(read_json(c_path))
        except Exception:  # noqa: BLE001 - a contract of another version: start again
            previous = None
    answers = load_answers(step._q_paths(engine)[1])
    by_unit, general = split_feedback(ctx.feedback, step.name)
    feedback = [*general, *by_unit.get("intent", [])]
    inputs_hash = sha256_json({"sources": [(s.path, s.sha256) for s in manifest.sources], "answers": answers, "behavioral": behavioral_fingerprint(raw)})
    reuse = bool(previous and C.frozen_ok(previous) and meta.get("intent_hash") == inputs_hash and not ctx.regenerate and not feedback)
    if c_path.is_file() and meta.get("contract_sha") and sha256_file(c_path) != meta["contract_sha"]:
        project.backup([c_path])  # edited by hand since it was written: never overwritten without a copy
        ctx.emit("warning", message="verification_contract.json was edited since Q3TUI wrote it: a copy is in .q3tui/bkp/")

    contract, problems = previous, []
    if not reuse:
        deny = [lay.rtl, lay.sva, lay.tb, lay.schemas, project.model_dir, project.arch_dir, project.rtl_dir, project.tb_dir]
        add = [p.parent for p in standards if project.root.resolve() not in p.resolve().parents]
        intent = None
        for attempt in range(ctx.cfg.tb.max_fix_attempts + 1):
            ctx.emit("log", message="deriving the verification contract from the approved specification …" if attempt == 0 else f"fixing the contract ({len(problems)} problem(s)) …")
            res = await ctx.llm(Stage(
                name="tb_intent", system_prompt=tasks.INTENT_SYSTEM, cwd=project.root, builtin_tools=["Read", "Grep", "Glob"], output_model=DvIntent,
                deny_dirs=deny, add_dirs=add, max_turns=ctx.cfg.tb.max_turns,
                prompt=tasks.intent_prompt(contract=draft.model_dump(mode="json") | ({"objectives": [o.model_dump() for o in previous.objectives],
                                                                                    "scoreboard_rules": [o.model_dump() for o in previous.scoreboard_rules],
                                                                                    "coverage": [o.model_dump() for o in previous.coverage],
                                                                                    "tests": [o.model_dump() for o in previous.tests],
                                                                                    "open_issues": [o.model_dump() for o in previous.open_issues]} if previous else {}),
                                           spec_files=[project.rel(p) for p in docs], answers=answers, settled=settled_block(engine.settled_questions(step.name)),
                                           feedback=feedback, problems=problems, previous=previous is not None, vip=opts.vip.kind if opts.vip else None)))
            intent = res.output
            contract, problems = apply_intent(draft, intent, previous, answered={k.removeprefix('Q-TB-') for k in answers if k.startswith('Q-TB-OI-')},
                                              keep_scope=not feedback)  # (a change request may reclassify an issue: "OI-004 is a spec gap")
            if not problems:
                break
        meta["intent_hash"] = inputs_hash
        issues_q = issue_questions(contract)
        own, repeats = drop_repeats(intent.questions, issues_q)
        for q in repeats:
            ctx.emit("log", message=f"question dropped, an open issue already asks it: {q.question[:80]}")
        _write_questions(step, ctx, [*own, *issues_q])
        if problems:
            raise StepFailed("the verification contract still has problems after the fix rounds:\n" + "\n".join(f"- {p}" for p in problems[:15]))
        artifacts.write(project, "golden_manifest.json", manifest.model_dump(mode="json"), check=False)
        blocking = [o for o in contract.open_issues if o.kind == "BLOCKED"]
        if not blocking:
            contract = C.freeze(contract, artifacts.now())
        artifacts.write(project, "verification_contract.json", contract.model_dump(mode="json"), check=False)
        meta["contract_sha"] = sha256_file(c_path)
    refresh_review(step, engine, contract)
    if contract.metadata.status != "frozen":
        ctx.emit("warning", message="the contract is not frozen: BLOCKED open issue(s) need your answer before the testbench can be built")
    return contract


def architecture_phase(step, ctx, opts: UvmVipOptions, contract) -> dict:
    """The `tb` step: the testbench architecture derived from the frozen contract, checked against it (schemas/tb_architecture.json, reports/tb_architecture.md)."""
    from q3tui.flows.dv.uvm import arch as arch_mod
    from q3tui.pipeline.base import StepFailed
    from q3tui.flows.spec.lib import artifacts

    project = ctx.project
    lay = step.lay(ctx.engine)
    architecture = arch_mod.derive(contract, opts.model_dump(mode="json"))
    bad = arch_mod.check(architecture, contract)
    if bad:
        raise StepFailed("the testbench architecture does not match its contract: " + "; ".join(bad))
    artifacts.write(project, "tb_architecture.json", architecture, check=False)
    (lay.reports).mkdir(parents=True, exist_ok=True)
    (lay.reports / "tb_architecture.md").write_text(arch_mod.render_md(architecture))
    return architecture


def vip_phase(step, ctx, opts: UvmVipOptions, contract) -> dict:
    """Probe the existing VIP and bind it to the interface contract (DUT-DV-010): schemas/vip_integration.json. The VIP is only read."""
    from q3tui.core.project import write_json
    from q3tui.flows.dv.uvm import binding as B
    from q3tui.flows.dv.uvm import vip as V
    from q3tui.pipeline.base import StepFailed
    from q3tui.flows.spec.lib import artifacts

    project = ctx.project
    vip_dir = resolve_path(opts.vip.path, project.root)
    # the artifact never carries this machine's absolute path (the configuration loader expands $VARs, so an absolute value is only a location)
    shown = opts.vip.path if ("$" in opts.vip.path or not Path(opts.vip.path).is_absolute()) else "<absolute location of this machine: see the uvm_vip.vip.path option>"
    probe = V.probe(vip_dir, opts.vip.kind, opts.vip.revision, path_expr=shown)
    bad = V.conformance(probe)
    if bad:
        raise StepFailed("the VIP cannot be used as it is (probe of " + opts.vip.path + "):\n" + "\n".join(f"- {b}" for b in bad))
    binding = B.derive(probe, contract, opts.integration.simulator)
    if not opts.vip.revision:
        binding["findings"].append({"id": f"BIND-{len(binding['findings']) + 1:03d}", "severity": "warning",
                                    "text": "uvm_vip.vip.revision is not set: the VIP is pinned only by the fingerprint of its files (vip.fingerprint)"})
    write_json(artifacts.path(project, "vip_integration.json"), {"schema_version": 1, "probe": probe, "binding": binding})
    if binding["blocking"]:
        raise StepFailed("the VIP does not match the DUT interface contract:\n" + "\n".join(f"- {f['text']}" for f in binding["blocking"]))
    ctx.emit("log", message=f"VIP {probe['roles']['agent']}: {len(binding['signal_map'])} signal(s) mapped, {len(binding['findings'])} finding(s), "
                            f"{len(binding['substitutions'])} substitution(s)")
    return {"probe": probe, "binding": binding, "root": (f"${opts.vip.env}" if opts.vip.env else (opts.vip.path if "$" in opts.vip.path else str(vip_dir))), "dir": vip_dir}


async def scaffold_phase(step, ctx, opts: UvmVipOptions, contract, architecture, vip: dict | None = None) -> dict:
    """Frozen contract and architecture to the UVM scaffold: files, worklist, validation, and the shell compile (no DUT, no VIP yet)."""
    from q3tui.core.project import write_json
    from q3tui.flows.dv.uvm import evidence as ev
    from q3tui.flows.dv.uvm import lint as lint_mod
    from q3tui.flows.dv.uvm import scaffold as sc
    from q3tui.flows.dv.uvm import worklist as wl
    from q3tui.pipeline.base import StepFailed
    from q3tui.flows.spec.lib import artifacts

    engine, project = ctx.engine, ctx.project
    lay = step.lay(engine)
    day = (contract.metadata.frozen_at or artifacts.now())[:10]
    files = sc.generate(contract, architecture, opts.model_dump(mode="json"), contract.metadata.ip_name, "project engineer", day, vip)
    meta = engine.state.step_meta.setdefault(step.name, {}).setdefault("uvm", {})
    res = sc.write(lay.tb, files, meta.get("scaffold") or {})
    meta["scaffold"] = {**(meta.get("scaffold") or {}), **res["sha"]}  # (the integration phase keeps its own files in the same record)
    for rel, ids in res["merged"].items():
        ctx.emit("log", message=f"{rel}: regenerated; kept your/agent regions: {', '.join(ids)}")
    for rel in res["kept_edited"]:
        ctx.emit("warning", message=f"{rel} was edited outside its DV regions: left untouched, the new scaffold is in {rel}.scaffold_new")
    items = wl.scan(lay.tb, contract)
    write_json(artifacts.path(project, "dv_worklist.json"), {"schema_version": 1, "contract_hash": contract.metadata.contract_hash,
                                                              "summary": wl.summary(items), "items": items})
    names = ({u["unit"] for u in vip["probe"]["compile"]["order"] if u["kind"] == "package"} | {c["name"] for c in vip["probe"]["classes"]}) if vip else None
    findings = lint_mod.lint(lay.tb, contract, list(files), names, (vip["root"], vip["dir"]) if vip else None)
    write_json(artifacts.path(project, "dv_validation.json"), {"schema_version": 1, "findings": findings,
                                                                "errors": sum(f["severity"] == "error" for f in findings)})
    errors = [f for f in findings if f["severity"] == "error"]
    if errors:
        raise StepFailed("the generated testbench fails its validators:\n" + "\n".join(f"- {f['code']} {f['message']}" for f in errors[:15]))
    ctx.emit("log", message=f"scaffold: {len(files)} file(s), {wl.summary(items)['open']} open DV_TODO region(s), validators clean")
    return {"files": res, "worklist": wl.summary(items)}


async def compile_tb(step, ctx, vip: dict | None, note: str, filelist: str = "filelist_tb.f", name: str | None = None, evidence: str = TB_EVIDENCE):
    """One compile of the testbench (with the VIP, against the interface shell) through the tool role `sim_uvm`. Returns (result, evidence entry), or
    (None, None) when the role is not configured / the tool is not found (NOT_RUN: nothing is recorded)."""
    import anyio

    from q3tui.eda import tools as T
    from q3tui.flows.dv.uvm import evidence as ev
    from q3tui.flows.spec.lib import artifacts

    engine, project = ctx.engine, ctx.project
    lay = step.lay(engine)
    ts = T.load_tools(ctx.cfg, project.root)
    if "sim_uvm" not in ts.roles or not T.available(ts, "sim_uvm"):
        ctx.emit("warning", message="tool role 'sim_uvm' is not configured or its tool was not found: the compile is NOT_RUN")
        return None, None
    work = lay.sim / "tb_uvm"
    work.mkdir(parents=True, exist_ok=True)
    absf = work / "filelist_tb_abs.f"
    lines = []
    for ln in [x.split("//")[0].strip() for x in (lay.tb / filelist).read_text().splitlines()]:
        if not ln:
            continue
        if vip:
            ln = ln.replace(vip["root"], str(vip["dir"]))
        lines.append(f"+incdir+{(lay.tb / ln[len('+incdir+'):]).resolve()}" if ln.startswith("+incdir+") else str((lay.tb / ln).resolve()))
    absf.write_text("\n".join(lines) + "\n")
    extra = ((vip or {}).get("binding", {}).get("compile_options", {}) or {}).get(opts_sim(step), [])
    n = len(list(work.glob("compile_*.log"))) + 1
    res = await anyio.to_thread.run_sync(lambda: T.run_role(ts, "sim_uvm", work, {"filelist": absf, "top": "tb_top", "workdir": work, "project": project.root,
                                                                                    "files": [], "incdirs": [], "extra_args": extra}, log_name=f"compile_{n:03d}.log"))
    entry = ev.record(project, "compile", name or ("tb + VIP + interface shell (no DUT)" if vip else "tb + interface shell (no DUT)"), res, note, artifacts.now(), file=evidence)
    ctx.emit("log", message=f"compile: {entry['status']} (rc={entry['returncode']}), log {entry['log']}")
    return res, entry


async def shell_compile(step, ctx, vip: dict | None = None) -> dict | None:
    """The compile of the generated testbench against the interface shell: a syntax and integration check, never a DUT result (DUT-DV-012)."""
    _, entry = await compile_tb(step, ctx, vip, "syntax and integration check against an interface shell: not a DUT result")
    return entry


async def semantic_phase(step, ctx, opts: UvmVipOptions, contract, vip: dict | None) -> dict:
    """The agent implements the smoke-path regions; code splices, validates and compiles them (fix rounds on the compiler's errors)."""
    import anyio

    from q3tui.flows.dv.uvm import lint as lint_mod
    from q3tui.flows.dv.uvm import semantic as sem
    from q3tui.flows.dv.uvm import task as tasks
    from q3tui.flows.dv.uvm import worklist as wl
    from q3tui.llm.runtime import Stage
    from q3tui.pipeline.base import StepFailed
    from q3tui.steps.common import load_answers, split_feedback

    engine, project = ctx.engine, ctx.project
    lay = step.lay(engine)
    meta = engine.state.step_meta.setdefault(step.name, {}).setdefault("uvm", {}).setdefault("semantic", {})
    by_unit, general = split_feedback(ctx.feedback, step.name)
    vip_names = ({u["unit"] for u in vip["probe"]["compile"]["order"] if u["kind"] == "package"} | {c["name"] for c in vip["probe"]["classes"]}) if vip else None
    deny = [lay.rtl, lay.sva, lay.schemas, project.model_dir, project.arch_dir, project.rtl_dir, project.tb_dir]
    from q3tui.flows.dv.uvm import review as rv

    decisions = []
    for i in rv.ledger(contract, load_answers(project.schemas_dir / "questions" / "testplan.answers.json"),  # (the issues are the testplan step's: its answers and accepted defaults)
                       __import__("q3tui.core.project", fromlist=["read_json"]).read_json(rv.decisions_path(project), default={}) or {},
                       dict(engine.state.accepted_defaults.get("testplan", {}))):
        if i["class"] in ("user_answered", "user_accepted_default"):
            decisions.append(f"{i['issue_id']} ({', '.join(i['requirement_ids'])}): {i['question']} -> " + (i.get("answer") or f"the default is accepted: {i.get('default', '')}"))
    problems_by_unit: dict[str, list[str]] = {}
    written_all: dict[str, list[str]] = {}
    llm_calls = 0

    async def run_unit(unit: dict, extra: list[str]) -> list[str]:
        nonlocal llm_calls
        items = wl.scan(lay.tb, contract)
        sctx = sem.unit_context(lay.tb, contract, unit, items)
        previous = sem.region_bodies(lay.tb, items, unit["regions"])
        probs = list(extra)
        for attempt in range(ctx.cfg.tb.max_fix_attempts + 1):
            ctx.emit("log", message=f"implementing {unit['name']} regions: {', '.join(unit['regions'])}" + (f" (fix round {attempt})" if attempt else "") + " …")
            res = await ctx.llm(Stage(name=f"tb_semantic_{unit['name']}", system_prompt=tasks.SEMANTIC_SYSTEM, cwd=project.root, builtin_tools=["Read", "Grep", "Glob"],
                                      output_model=sem.SemanticResult, deny_dirs=deny, max_turns=ctx.cfg.tb.max_turns,
                                      prompt=sem.prompt(sctx, decisions, previous, probs, [*general, *by_unit.get(unit["name"], [])])))
            llm_calls += 1
            probs, written = sem.apply(lay.tb, items, res.output.regions, unit["regions"], vip_names)
            written_all.setdefault(unit["name"], []).extend(written)
            previous = sem.region_bodies(lay.tb, wl.scan(lay.tb, contract), unit["regions"])
            if not probs:
                return []
        return probs

    todo = []
    items = wl.scan(lay.tb, contract)
    for unit in sem.units(contract, opts.semantic.scope, items):
        sctx = sem.unit_context(lay.tb, contract, unit, items)
        h = sem.input_hash(sctx)
        owners = [i["owner"] for i in items if i["id"] in unit["regions"]]
        done = meta.get(unit["name"]) == h and all(o != "scaffold" for o in owners) and not general and unit["name"] not in by_unit and not ctx.regenerate
        if done:
            ctx.emit("log", message=f"{unit['name']}: unchanged since it was implemented (no LLM)")
        else:
            todo.append((unit, h))
    for unit, h in todo:  # one unit after the other: both edit files of the same package, and the compile that follows covers both
        left = await run_unit(unit, [])
        if left:
            raise StepFailed(f"the {unit['name']} regions still have problems after the fix rounds:\n" + "\n".join(f"- {p}" for p in left[:15]))
        meta[unit["name"]] = sem.input_hash(sem.unit_context(lay.tb, contract, unit, wl.scan(lay.tb, contract)))  # kept even if a later unit fails: no paid work is repeated
    # compile (with fix rounds on the compiler's errors in testbench files)
    res = entry = None
    for attempt in range(ctx.cfg.tb.max_fix_attempts + 1):
        res, entry = await compile_tb(step, ctx, vip, "syntax and integration check against an interface shell: not a DUT result" + (f" (after fix round {attempt})" if attempt else ""))
        if res is None or res.ok:
            break
        errs = [d for d in res.diagnostics if d.severity == "error"]
        mine: dict[str, list[str]] = {}
        outside = []
        for d in errs:
            try:
                rel = str(Path(d.file).resolve().relative_to(lay.tb.resolve())) if d.file else ""
            except ValueError:
                rel = ""
            (mine.setdefault(rel, []) if rel else outside).append(f"{d.code}{f' line {d.line}' if d.line else ''}: {d.message}" if rel else f"{d.file}:{d.line}: {d.message}")
        if not mine or attempt == ctx.cfg.tb.max_fix_attempts:
            raise StepFailed("the testbench does not compile (log " + (entry["log"] if entry else "") + "):\n" + "\n".join(f"- {x}" for x in [*sum(mine.values(), []), *outside][:12]))
        for unit in sem.units(contract, opts.semantic.scope, wl.scan(lay.tb, contract)):
            items = wl.scan(lay.tb, contract)
            files = {i["file"] for i in items if i["id"] in unit["regions"]}
            hit = [f for f in mine if f in files]
            if hit:
                msgs = [f"{f}: {m}" for f in hit for m in mine[f]]
                left = await run_unit(unit, msgs)
                if left:
                    raise StepFailed(f"the {unit['name']} regions still have problems after the fix rounds:\n" + "\n".join(f"- {p}" for p in left[:15]))
    for unit, h in todo:
        items = wl.scan(lay.tb, contract)
        meta[unit["name"]] = sem.input_hash(sem.unit_context(lay.tb, contract, unit, items))
    items = wl.scan(lay.tb, contract)
    from q3tui.core.project import write_json
    from q3tui.flows.spec.lib import artifacts

    write_json(artifacts.path(project, "dv_worklist.json"), {"schema_version": 1, "contract_hash": contract.metadata.contract_hash, "summary": wl.summary(items), "items": items})
    findings = lint_mod.lint(lay.tb, contract, [str(p.relative_to(lay.tb)) for p in lay.tb.rglob("*") if p.is_file() and p.suffix in (".sv", ".svh", ".f")], vip_names,
                             (vip["root"], vip["dir"]) if vip else None)
    write_json(artifacts.path(project, "dv_validation.json"), {"schema_version": 1, "findings": findings, "errors": sum(f["severity"] == "error" for f in findings)})
    errs = [f for f in findings if f["severity"] == "error"]
    if errs:
        raise StepFailed("the testbench fails its validators after the implementation:\n" + "\n".join(f"- {f['code']} {f['message']}" for f in errs[:15]))
    ctx.emit("log", message=f"semantic: {sum(len(v) for v in written_all.values())} region(s) written by the agent in {llm_calls} call(s); {wl.summary(items)['open']} region(s) still open")
    return {"written": written_all, "llm_calls": llm_calls, "worklist": wl.summary(items)}


async def integration_phase(step, ctx, opts: UvmVipOptions, contract, architecture, vip: dict | None) -> dict:
    """The separate RTL phase (DUT-DV-026): only now is the RTL read, by code, for structure and compilation: top, ports, parameters, files.
    The contract and the architecture are frozen and are checked unchanged afterwards. A mismatch is a conformance finding and stops the step."""
    import os

    from q3tui.core.project import sha256_file, write_json
    from q3tui.flows.dv.uvm import contract as C
    from q3tui.flows.dv.uvm import integrate as I
    from q3tui.flows.dv.uvm import lint as lint_mod
    from q3tui.flows.dv.uvm import scaffold as sc
    from q3tui.flows.dv.uvm import spec_facts as sf
    from q3tui.flows.dv.uvm import svgen as g
    from q3tui.pipeline.base import StepFailed
    from q3tui.flows.spec.lib import artifacts

    engine, project = ctx.engine, ctx.project
    lay = step.lay(engine)
    if not C.frozen_ok(contract) or architecture.get("contract_hash") != contract.metadata.contract_hash:
        raise StepFailed("the RTL integration needs the frozen verification contract and the architecture derived from it (DUT-DV-026)")
    frozen = {n: sha256_file(artifacts.path(project, n)) for n in ("verification_contract.json", "tb_architecture.json", "golden_manifest.json")}
    inp = opts.integration
    rtl_dir = (project.root / inp.rtl_dir).resolve()
    if not rtl_dir.is_dir():
        raise StepFailed(f"integration.rtl_dir not found: {inp.rtl_dir}")
    fl = rtl_dir / "filelist.f"
    files, incs, defs = I.read_filelist(fl, project.root) if fl.is_file() else (sorted(rtl_dir.glob("*.sv")), [], {})
    defs = {**defs, **inp.defines}
    incs += [(project.root / d).resolve() for d in inp.incdirs]
    top = inp.top or contract.interface_contract.top_module
    spec_md = "\n".join(p.read_text(errors="replace") for p in project.spec_documents() if p.suffix.lower() == ".md")
    names = sf.original_names(spec_md)
    overrides = {inp.param_map.get(p.name, p.name): p.value for p in contract.interface_contract.parameters}
    rtl = I.read_interface(files, top, incs, defs, overrides)
    findings = I.conformance(contract, rtl, top, inp.param_map, inp.port_map, names)
    errors = [f for f in findings if f["severity"] == "error"]
    rel = lambda p: os.path.relpath(p, lay.tb)  # noqa: E731
    data = {"schema_version": 1, "contract_hash": contract.metadata.contract_hash, "frozen_inputs": frozen,
            "rtl": {"top": top, "files": [str(p.relative_to(project.root)) for p in files], "incdirs": [str(p.relative_to(project.root)) for p in incs], "defines": defs,
                    "read_for": "structure and compilation only (DUT-DV-004): top module, ports, parameters, elaboration diagnostics, file list",
                    "ports": rtl["ports"], "parameters": rtl["parameters"], "diagnostics": rtl["diagnostics"]},
            "mappings": {"param_map": inp.param_map, "port_map": inp.port_map}, "conformance": {"ok": not errors, "findings": findings},
            "wrapper": None if errors else "integration/dut_wrapper.sv", "filelist": None if errors else "filelist_dut.f"}
    write_json(artifacts.path(project, "dut_integration.json"), data)
    lay.reports.mkdir(parents=True, exist_ok=True)
    (lay.reports / "dut_conformance_report.md").write_text(
        f"# DUT conformance report — {contract.metadata.ip_name}\n\nThe RTL was read for structure and compilation only. Expected behavior is the approved specification; "
        f"a difference is reported, never accommodated (DUT-DV-005).\n\nTop `{top}`; {len(rtl['ports'])} port(s), {len(rtl['parameters'])} parameter(s); "
        f"{'CONFORMS' if not errors else 'DOES NOT CONFORM'} ({len(errors)} error(s), {len(findings) - len(errors)} warning(s)).\n\n"
        + "".join(f"- **{f['severity'].upper()} {f['id']}** `{f['code']}`: {f['text']}\n" for f in findings))
    if errors:
        mp = [f for f in errors if f["code"] == "parameter_missing"]
        hint = ""
        if mp and all(p.name in names and names[p.name] in {x["name"] for x in rtl["parameters"]} for p in contract.interface_contract.parameters):
            hint = ("\nThe specification lists these names itself (its `Original name` column) and the RTL has them. To approve that mapping, set in the tb options:\n"
                    "  integration:\n    param_map: {" + ", ".join(f"{k}: {v}" for k, v in names.items()) + "}")
        raise StepFailed(f"the RTL does not conform to the approved interface contract ({len(errors)} error(s); reports/dut_conformance_report.md):\n"
                         + "\n".join(f"- {f['text']}" for f in errors[:8]) + hint)
    day = (contract.metadata.frozen_at or artifacts.now())[:10]
    hdr = lambda d: g.file_header(contract.metadata.ip_name, "project engineer", day, d, contract.metadata.contract_hash)  # noqa: E731
    tbfl = (lay.tb / "filelist_tb.f").read_text()
    extra = {"integration/dut_wrapper.sv": I.wrapper(contract, rtl, top, hdr("The real DUT wrapper."), inp.param_map, inp.port_map),
             "filelist_dut.f": I.dut_filelist(tbfl, [rel(p) for p in files])}
    meta = engine.state.step_meta.setdefault(step.name, {}).setdefault("uvm", {})
    res = sc.write(lay.tb, extra, meta.get("scaffold") or {})
    meta["scaffold"] = {**(meta.get("scaffold") or {}), **res["sha"]}
    allfiles = [str(p.relative_to(lay.tb)) for p in lay.tb.rglob("*") if p.is_file() and p.suffix in (".sv", ".svh", ".f")]
    names_v = ({u["unit"] for u in vip["probe"]["compile"]["order"] if u["kind"] == "package"} | {c["name"] for c in vip["probe"]["classes"]}) if vip else None
    vf = lint_mod.lint(lay.tb, contract, allfiles, names_v, (vip["root"], vip["dir"]) if vip else None)
    bad = [f for f in vf if f["severity"] == "error" and f["code"] != "DV-F02"]
    if bad:
        raise StepFailed("the integrated testbench fails its validators:\n" + "\n".join(f"- {f['code']} {f['message']}" for f in bad[:10]))
    if {n: sha256_file(artifacts.path(project, n)) for n in frozen} != frozen:
        raise StepFailed("a frozen behavioral artifact changed during the RTL integration (DUT-DV-026)")
    ctx.emit("log", message=f"RTL integration: top {top}, {len(rtl['ports'])} port(s) conform, {len(files)} RTL file(s); wrapper and filelist_dut.f written")
    res_c, entry = await compile_tb(step, ctx, vip, "compile and elaboration of the testbench with the released RTL as the DUT (no simulation yet)", "filelist_dut.f",
                                    "tb + VIP + RTL (DUT)")
    return {"conforms": True, "compile": entry}


async def run_one_test(step, ctx, test: str, seed: int, note: str) -> dict | None:
    """Run one test with the tool role `run_uvm` on the simv built by the last compile (the DUT compile). The status comes from the log only
    (DUT-DV-012, 023): exit code 0, zero UVM_ERROR and UVM_FATAL, a PASS result line for this test and no DV_TODO announced. Never invented."""
    import anyio

    from q3tui.eda import tools as T
    from q3tui.flows.dv.uvm import evidence as ev
    from q3tui.flows.spec.lib import artifacts

    engine, project = ctx.engine, ctx.project
    lay = step.lay(engine)
    ts = T.load_tools(ctx.cfg, project.root)
    work = lay.sim / "tb_uvm"
    if "run_uvm" not in ts.roles or not T.available(ts, "run_uvm"):
        ctx.emit("warning", message="tool role 'run_uvm' is not configured or its tool was not found: the simulation is NOT_RUN")
        return None
    res = await anyio.to_thread.run_sync(lambda: T.run_role(ts, "run_uvm", work, {"test": test, "seed": seed, "workdir": work, "project": project.root},
                                                            log_name=f"run_{test}_seed{seed}.log"))
    text = Path(res.log_path).read_text(errors="replace") if res.log_path and Path(res.log_path).is_file() else ""
    status, why = ev.judge_run(text, res.returncode, test)
    entry = ev.record(project, "run", test, res, why or note, artifacts.now(), status=status, seed=seed)
    ctx.emit("log", message=f"simulation {test} seed {seed}: {status}" + (f" ({why})" if why else "") + f"; UVM_ERROR={entry['uvm_error']} UVM_FATAL={entry['uvm_fatal']}; log {entry['log']}")
    return entry


async def smoke_phase(step, ctx, opts: UvmVipOptions) -> dict | None:
    """The deterministic smoke test (DUT-DV-015)."""
    return await run_one_test(step, ctx, opts.integration.smoke_test, 1, "smoke test with the released RTL as the DUT")


async def tests_phase(step, ctx, opts: UvmVipOptions, contract) -> list[dict]:
    """Run every directed test whose body the agent implemented (an unimplemented one stays a todo and is not run). One entry per test; the
    table reports/dv_test_results.md lists them with the tests that were not run (status `todo`)."""
    from q3tui.flows.dv.uvm import svgen as g
    from q3tui.flows.dv.uvm import worklist as wl

    lay = step.lay(ctx.engine)
    owner = {i["id"]: i["owner"] for i in wl.scan(lay.tb, contract)}
    rows, entries = [], []
    for t in contract.tests:
        if t.kind == "smoke" and t.milestone == "v0_1":
            continue
        cls = f"dut_tc_{int(t.tc_id.split('-')[-1]):03d}_{g.lc(t.name)}_test"
        if owner.get(f"test.{t.tc_id}", "scaffold") == "scaffold":
            rows.append((t.tc_id, cls, "todo", "not implemented"))
            continue
        e = await run_one_test(step, ctx, cls, 1, f"{t.tc_id} with the released RTL as the DUT")
        if e is None:
            rows.append((t.tc_id, cls, "NOT_RUN", "tool role run_uvm is not available"))
            continue
        entries.append(e)
        incomplete = e["status"] != "PASS" and e["note"].startswith("result line says INCOMPLETE")  # ran clean but made no check: no evidence either way
        rows.append((t.tc_id, cls, "INCOMPLETE" if incomplete else e["status"], e["note"] or f"UVM_ERROR={e['uvm_error']} UVM_FATAL={e['uvm_fatal']}; log {e['log']}"))
    lay.reports.mkdir(parents=True, exist_ok=True)
    (lay.reports / "dv_test_results.md").write_text("# Directed test results (released RTL as the DUT)\n\nStatus is read from the simulator log of each run (`schemas/dv_evidence.json`); "
                                                 "`todo` = not implemented, not run; `INCOMPLETE` = ran without error but made no check.\n\n| Test | Class | Status | Note |\n|---|---|---|---|\n"
                                                 + "".join(f"| {a} | `{b}` | {c} | {d} |\n" for a, b, c, d in rows))
    return entries


def frozen_inputs(project):
    """(contract, architecture) written by the steps before, or (None, why) when they are not there (not run, or the contract is not frozen)."""
    from q3tui.core.project import read_json
    from q3tui.flows.dv.uvm import contract as C
    from q3tui.flows.spec.lib import artifacts

    c_path = artifacts.path(project, "verification_contract.json")
    if not c_path.is_file():
        return None, "the verification contract is needed first (step testplan: schemas/verification_contract.json)"
    try:
        contract = C.VerificationContract.model_validate(read_json(c_path))
    except Exception:  # noqa: BLE001 - a contract of another version: the testplan step writes it again
        return None, "schemas/verification_contract.json is not readable (run step testplan again)"
    if not C.frozen_ok(contract):
        return None, "the verification contract is not frozen: answer the open issues of step testplan first (DUT-DV-026)"
    return contract, ""


async def run_testplan(step, ctx) -> None:
    """Entry of the `testplan` step: the verification contract (code drafts it, the `tb_intent` stage completes it, code validates and freezes it). The open
    issues the specification leaves are questions for you, with their review report; the step never reads the RTL."""
    from q3tui.pipeline.base import StepFailed

    raw = tb_options(ctx.engine)
    problem = input_problem(raw, ctx.project.root)
    if problem:
        raise StepFailed(problem)
    await contract_phase(step, ctx, parse_options(raw), raw)


async def run_testbench(step, ctx) -> None:
    """Entry of the `tb` step: from the frozen contract the architecture, the VIP binding, the scaffold and the agent's implementation of the regions (compiled
    against an interface shell; the stages never see the RTL, rule DUT-DV-026), then the released RTL as the DUT: read by code for structure and compilation,
    checked against the contract (a difference is a finding, never adapted to), the wrapper and file list, and the compile with the RTL."""
    from q3tui.pipeline.base import StepFailed

    problem = input_problem(step.options, ctx.project.root)
    if problem:
        raise StepFailed(problem)
    opts = parse_options(step.options)
    contract, why = frozen_inputs(ctx.project)
    if contract is None:
        raise StepFailed(why)
    architecture = architecture_phase(step, ctx, opts, contract)
    vip = vip_phase(step, ctx, opts, contract) if opts.vip else None  # (no VIP: the interface shell and the pins only; an interface that needs one was refused above)
    await scaffold_phase(step, ctx, opts, contract, architecture, vip)
    await semantic_phase(step, ctx, opts, contract, vip)
    integ = await integration_phase(step, ctx, opts, contract, architecture, vip)
    if integ["compile"] is None:
        raise StepFailed("the testbench was not compiled with the RTL (tool role sim_uvm is not available): nothing was simulated")
    if integ["compile"]["status"] != "PASS":
        raise StepFailed("the testbench does not compile with the RTL (see " + integ["compile"]["log"] + ")")


async def run_simulate(step, ctx) -> None:
    """Entry of the `simulate` step: the smoke test and, with `semantic.scope: all`, the directed tests, run with the tool role `run_uvm` on the simv the `tb`
    step built. The status of every run is read from the simulator log only (rules DUT-DV-012, 023)."""
    from q3tui.pipeline.base import StepFailed

    raw = tb_options(ctx.engine)
    problem = input_problem(raw, ctx.project.root)
    if problem:
        raise StepFailed(problem)
    opts = parse_options(raw)
    contract, why = frozen_inputs(ctx.project)
    if contract is None:
        raise StepFailed(why)
    from q3tui.flows.dv import triage

    lay = step.lay(ctx.engine)
    smoke = await smoke_phase(step, ctx, opts)
    if smoke is None:
        raise StepFailed("the smoke simulation is NOT_RUN (tool role run_uvm is not available)")
    if smoke["status"] != "PASS":
        original = (f"the smoke test {smoke['name']} did not pass: {smoke['note']} (log {smoke['log']}; UVM_ERROR={smoke['uvm_error']}, UVM_FATAL={smoke['uvm_fatal']}). "
                    "The expected behavior comes from the approved specification; a failure is a finding about the DUT or the testbench, never a reason to change it")
        await triage.handle_failures(step, ctx, [{"id": smoke["name"], "name": "smoke test", "note": smoke["note"], "log": smoke["log"], "reqs": []}], lay.rtl, "uvm", original)
        return
    if opts.semantic.scope == "all":
        entries = await tests_phase(step, ctx, opts, contract)
        for e in entries:
            if e["status"] != "PASS" and e["note"].startswith("result line says INCOMPLETE"):
                ctx.emit("warning", message=f"{e['name']} made no check (INCOMPLETE): it is not evidence for its requirements")
        failed = [e for e in entries if e["status"] != "PASS" and not e["note"].startswith("result line says INCOMPLETE")]
        if failed:
            original = ("directed tests that did not pass (details: reports/dv_test_results.md): " + ", ".join(f"{e['name']} ({e['note']})" for e in failed)
                        + ". The expected behavior comes from the approved specification; a failure is a finding about the DUT or the testbench, never a reason to change it")
            await triage.handle_failures(step, ctx, [{"id": e["name"], "name": "", "note": e["note"], "log": e["log"], "reqs": []} for e in failed], lay.rtl, "uvm", original)
            return
    triage.reset(ctx.engine, step)


def opts_sim(step) -> str:
    return parse_options(step.options).integration.simulator
