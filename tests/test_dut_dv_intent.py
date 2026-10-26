"""Milestone M2 of the `uvm_vip` backend: approved spec -> frozen verification contract and TB architecture, on the axi4_sram_ctrl
example. Fake LLM, no tool, no RTL."""

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

from q3tui.core.project import Project
from q3tui.flows.dv.uvm import arch as arch_mod
from q3tui.flows.dv.uvm import contract as C
from q3tui.flows.dv.uvm import spec_facts as sf
from q3tui.flows.dv.uvm.intent import DvIntent, ReqDecision, apply_intent
from q3tui.llm import runtime
from q3tui.pipeline.engine import Engine
from q3tui.flows.spec.lib import artifacts
from tests.fakes import FakeLLM
from tests.vlsit_fakes import register

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "axi4_sram_ctrl"
FLOW = ("name: t\nsteps:\n  - {id: testplan, kind: dv_testplan, gate: auto, deps: []}\n"
        "  - {id: tb, kind: dv_tb, gate: auto, deps: [testplan]}\n"
        "  - {id: simulate, kind: dv_simulate, gate: auto, deps: [tb]}\n")


def example_draft() -> C.VerificationContract:
    st = json.loads((EXAMPLE / "schemas" / "structured_spec.json").read_text())
    cf = json.loads((EXAMPLE / "schemas" / "final_config.json").read_text())
    return C.draft_contract(st, cf, {"spec/spec.md": (EXAMPLE / "spec" / "spec.md").read_text()})


def good_intent(draft: C.VerificationContract) -> DvIntent:
    """What a competent intent stage returns for the draft (built by the test from the draft's own flags)."""
    dec, plan_reqs, assumed = [], [], []
    for r in draft.requirements:
        if r.origin_class == "ASSUMED_CANDIDATE":
            dec.append(ReqDecision(req_id=r.req_id, observability="PORT", origin="ASSUMED", status="open", status_reason="the specification marks this as an assumption"))
            assumed.append(r.req_id)
        elif r.observability_hint == "INTERNAL":
            dec.append(ReqDecision(req_id=r.req_id, observability="INTERNAL", status="not_observable", status_reason="describes the inside of the design"))
        elif r.observability_hint == "ELAB_CONFIG":
            dec.append(ReqDecision(req_id=r.req_id, observability="ELAB_CONFIG", status="planned"))
            plan_reqs.append(("VO-002", r.req_id))
        else:
            dec.append(ReqDecision(req_id=r.req_id, observability="PORT", status="planned"))
            plan_reqs.append(("VO-001", r.req_id))
    vo1 = [r for v, r in plan_reqs if v == "VO-001"]
    vo2 = [r for v, r in plan_reqs if v == "VO-002"]
    return DvIntent(
        decisions=dec,
        interfaces=[{"id": "IF-AXI", "kind": "axi4_slave", "protocol": "AXI4", "role": "slave", "ports": ["i_awaddr"], "stimulus_agent": "vip_master"},
                    {"id": "IF-SRAM", "kind": "sram", "role": "memory master", "ports": ["o_sram_we"], "stimulus_agent": "sram_model"}],
        objectives=[{"id": "VO-001", "title": "observable behavior", "requirement_ids": vo1},
                    {"id": "VO-002", "title": "legal parameter sets only", "requirement_ids": vo2}],
        scoreboard_rules=[{"id": "SBR-001", "description": "a write then a read of the same word returns the data", "requirement_ids": vo1[:3],
                           "source": "spec/spec.md section 5.7 and 5.8", "in_smoke": True}],
        coverage=[{"id": "COV-001", "description": "burst types", "objective_ids": ["VO-001"]}],
        tests=[{"tc_id": "TC-001", "name": "smoke_write_read", "kind": "smoke", "milestone": "v0_1", "requirement_ids": vo1[:3], "rule_ids": ["SBR-001"]}],
        open_issues=[{"id": "OI-001", "kind": "OPEN", "requirement_ids": assumed, "question": "Do outputs read 0 after reset?",
                      "source": "spec/spec.md lines 110-192", "default_assumption": "not checked"}])


@pytest.fixture
def llm(monkeypatch):
    fake = FakeLLM()
    fake.dv_intents = []   # queue of DvIntent to return (else the good one)
    fake.dv_semantic = []  # queue of SemanticResult (else a body that only holds a comment for every region of the task)
    @register
    def handler(f, stage, emit):
        if f is fake and stage.name.startswith("tb_semantic_"):
            from q3tui.flows.dv.uvm.semantic import RegionImpl, SemanticResult
            import re as _re
            out = f.dv_semantic.pop(0) if f.dv_semantic else SemanticResult(regions=[RegionImpl(id=i, body=f"// fake agent: {i}") for i in _re.findall(r"## Region `([^`]+)`", stage.prompt)])
            return runtime.StageResult("ok", out, 0.0, 1, "s")
        if stage.name != "tb_intent" or f is not fake:
            return None
        out = f.dv_intents.pop(0) if f.dv_intents else good_intent(example_draft())
        return runtime.StageResult("ok", out, 0.0, 1, "s")
    monkeypatch.setattr(runtime, "run_stage", fake)
    return fake


@pytest.fixture
def engine(tmp_path):
    for d in ("spec", "schemas"):
        shutil.copytree(EXAMPLE / d, tmp_path / d)
    shutil.rmtree(tmp_path / "schemas" / "questions")  # the example's recorded review decisions are used by the review tests only
    (tmp_path / "schemas" / "dv_review_decisions.json").unlink()
    for n in ("golden_manifest", "verification_contract", "tb_architecture"):  # the example's reviewed baseline: only the baseline test keeps it
        (tmp_path / "schemas" / f"{n}.json").unlink()
    shutil.copytree(EXAMPLE.parent.parent / "tests" / "fixtures" / "mini_axi_vip", tmp_path / "vip")  # a small stand-in with the structure of an AXI VIP
    (tmp_path / "src" / "rtl").mkdir(parents=True)
    from tests.dutdv_rtl import mini_rtl

    (tmp_path / "src" / "rtl" / "dut.sv").write_text(mini_rtl())  # a conforming stand-in top (carries RTL_SENTINEL_TOKEN)
    (tmp_path / "q3tui.yaml").write_text("pipeline:\n  flow: t\n  step_options:\n    tb:\n      uvm_vip: {vip: {path: vip, kind: axi4_avip}}\n"
                                         "tools:\n  roles:\n    sim: {cmd: 'true', parse: vcs}\n")
    (tmp_path / "flows").mkdir()
    (tmp_path / "flows" / "t.yaml").write_text(FLOW)
    return Engine(Project.open(tmp_path))


def run_tb(eng, **kw):
    """The dv flow: the test plan (the verification contract), then the testbench with the RTL as its DUT, then the simulations: each step runs when what it
    needs is there (a contract with BLOCKED issues is not frozen: nothing is built from it)."""
    anyio.run(lambda: eng.run_step(eng.by_name["testplan"], **kw))
    for name in ("tb", "simulate"):
        step = eng.by_name[name]
        if step.missing_input(eng) is None:
            anyio.run(lambda: eng.run_step(step, **kw))


def load_contract(root: Path) -> C.VerificationContract:
    return C.VerificationContract.model_validate(json.loads((root / "schemas" / "verification_contract.json").read_text()))


# -- deterministic facts ---------------------------------------------------------------------------------------------

def test_spec_facts_from_the_example_spec():
    md = (EXAMPLE / "spec" / "spec.md").read_text()
    names = {p.name for p in sf.ports(md)}
    assert {"i_clk", "i_rst_n", "i_awaddr", "o_bvalid", "o_sram_we", "i_sram_rdata"} <= names and len(names) == 37
    notes = sf.assumptions(md)
    assert len(notes) >= 20 and any("reset" in a.topic for a in notes if a.kind == "cell")


def test_draft_flags_restated_assumptions_and_internals():
    d = example_draft()
    by = {r.req_id: r for r in d.requirements}
    assert by["REQ-042"].origin_class == "ASSUMED_CANDIDATE" and by["REQ-049"].origin_class == "ASSUMED_CANDIDATE"
    assert any(e.endswith(":192") for e in by["REQ-042"].assumption_evidence)
    assert by["REQ-033"].observability_hint == "INTERNAL" and by["REQ-001"].observability_hint == "PORT"
    assert by["REQ-059"].observability_hint == "ELAB_CONFIG" and by["REQ-059"].origin_class == "ASSUMED_CANDIDATE"
    ic = d.interface_contract
    assert ic.top_module == "m_vlsi_axi4_sram" and ic.clock == "i_clk" and ic.reset == "i_rst_n" and ic.reset_active_low
    assert {c.id for c in ic.constraints if c.assumed} == {"C2", "C8"}
    assert all(r.status == "draft" for r in d.requirements)


def test_manifest_pins_sources_and_excludes_structure():
    st = json.loads((EXAMPLE / "schemas" / "structured_spec.json").read_text())
    cf = json.loads((EXAMPLE / "schemas" / "final_config.json").read_text())
    m = C.build_manifest(EXAMPLE, [EXAMPLE / "spec" / "spec.md"], EXAMPLE / "schemas" / "structured_spec.json", EXAMPLE / "schemas" / "final_config.json", [], st, cf)
    kinds = {s.kind: s for s in m.sources}
    assert kinds["structured_spec"].approved and kinds["final_config"].approved and len(kinds["design_spec"].sha256) == 64
    assert any("rtl_modules" in e.where for e in m.excluded) and any("modules" == e.where.split(": ")[-1] for e in m.excluded)


def test_draft_contract_never_carries_rtl_structure():
    dump = example_draft().model_dump_json()
    assert "rtl_modules" not in dump and "module_order" not in dump


# -- applying the intent --------------------------------------------------------------------------------------------

def test_good_intent_gives_a_valid_traced_contract():
    d = example_draft()
    c, problems = apply_intent(d, good_intent(d))
    assert problems == []
    by = {r.req_id: r for r in c.requirements}
    assert by["REQ-042"].status == "open" and by["REQ-042"].origin_class == "ASSUMED" and by["REQ-033"].status == "not_observable"
    assert "TC-001" in by[c.tests[0].requirement_ids[0]].test_ids and "SBR-001" in by[c.tests[0].requirement_ids[0]].rule_ids


def test_missing_and_dangling_and_assumption_problems():
    d = example_draft()
    i = good_intent(d)
    i.decisions = [x for x in i.decisions if x.req_id != "REQ-001"]
    i.tests[0].requirement_ids.append("REQ-999")
    _, problems = apply_intent(d, i)
    assert any("no decision for: REQ-001" in p for p in problems) and any("dangling requirement ID REQ-999" in p for p in problems)
    # an assumption promoted to a planned expectation is refused (DUT-DV-006)
    i = good_intent(d)
    for x in i.decisions:
        if x.req_id == "REQ-042":
            x.origin, x.status, x.status_reason = "ASSUMED", "planned", ""
    _, problems = apply_intent(d, i)
    assert any("REQ-042" in p and "not golden" in p for p in problems)
    # an undecided assumption candidate and an internal requirement planned for a test
    i = good_intent(d)
    for x in i.decisions:
        if x.req_id == "REQ-049":
            x.origin = None
        if x.req_id == "REQ-033":
            x.observability, x.status = "INTERNAL", "planned"
    _, problems = apply_intent(d, i)
    assert any("REQ-049" in p and "may restate" in p for p in problems) and any("REQ-033" in p and "internal structure" in p for p in problems)


def test_ids_are_permanent_and_traced():
    d = example_draft()
    c, _ = apply_intent(d, good_intent(d))
    i = good_intent(d)
    i.tests = []
    _, problems = apply_intent(d, i, previous=c)
    assert any("permanent ID(s) dropped: TC-001" in p for p in problems) and any("no deterministic smoke test" in p for p in problems)
    i = good_intent(d)
    i.coverage[0].objective_ids = []
    i.scoreboard_rules[0].source = ""
    _, problems = apply_intent(d, i)
    assert any("COV-001" in p and "DUT-DV-009" in p for p in problems) and any("SBR-001" in p and "DUT-DV-008" in p for p in problems)


def test_freeze_detects_any_change():
    d = example_draft()
    c, _ = apply_intent(d, good_intent(d))
    f = C.freeze(c, "t")
    assert C.frozen_ok(f)
    changed = f.model_copy(update={"tests": [f.tests[0].model_copy(update={"name": "other"})]})
    assert not C.frozen_ok(changed)


def test_architecture_follows_the_frozen_contract():
    d = example_draft()
    c = C.freeze(apply_intent(d, good_intent(d))[0], "t")
    a = arch_mod.derive(c, {"vip": {"kind": "axi4_avip"}, "integration": {"simulator": "vcs", "smoke_test": "dut_smoke_test"}})
    assert arch_mod.check(a, c) == []
    assert a["smoke_test"]["tc_id"] == "TC-001" and a["dut"]["top_module"] == "m_vlsi_axi4_sram" and a["integration"]["status"] == "pending"
    assert [i["vip_binding"] for i in a["interfaces"]] == [{"status": "pending", "family": "axi4_avip"}, None]
    assert arch_mod.check({**a, "contract_hash": "x"}, c)
    assert "tb_top" in arch_mod.render_md(a)


# -- the step ---------------------------------------------------------------------------------------------------------

def test_step_freezes_contract_and_architecture_without_rtl(engine, llm):
    root = engine.project.root
    with pytest.raises(Exception, match="NOT_RUN|nothing was simulated"):  # the scaffold stages come with M3
        run_tb(engine)
    c = load_contract(root)
    assert c.metadata.status == "frozen" and C.frozen_ok(c)
    a = json.loads((root / "schemas" / "tb_architecture.json").read_text())
    assert a["contract_hash"] == c.metadata.contract_hash and (root / "reports" / "tb_architecture.md").is_file()
    assert json.loads((root / "schemas" / "golden_manifest.json").read_text())["excluded"]
    # RTL independence: the stage may not read the RTL, its prompt carries none of it
    st = llm.calls[0]
    assert st.name == "tb_intent" and root / "src" / "rtl" in st.deny_dirs and root / "src" / "sva" in st.deny_dirs and root / "src" / "tb" in st.deny_dirs
    assert st.builtin_tools == ["Read", "Grep", "Glob"] and "RTL_SENTINEL_TOKEN" not in st.prompt and "m_sentinel_rtl_only" not in st.prompt
    assert "rtl_modules" not in st.prompt
    # the open issue about the assumption became a question
    qs = json.loads((root / "schemas" / "questions" / "testplan.json").read_text())
    assert any("reset" in q["question"] and not q["blocking"] for q in qs)


def test_second_run_is_an_update_not_a_rerun(engine, llm):
    with pytest.raises(Exception, match="NOT_RUN|nothing was simulated"):
        run_tb(engine)
    n = len(llm.calls)
    with pytest.raises(Exception, match="NOT_RUN|nothing was simulated"):
        run_tb(engine)
    assert len(llm.calls) == n  # nothing changed: no LLM call


def test_unapproved_spec_is_refused(engine, llm):
    p = engine.project.root / "schemas" / "structured_spec.json"
    d = json.loads(p.read_text())
    d["metadata"]["gate_status"] = "pending"
    p.write_text(json.dumps(d))
    with pytest.raises(Exception, match="not approved"):
        run_tb(engine)
    assert not llm.calls


def test_fix_round_repairs_a_bad_intent(engine, llm):
    bad = good_intent(example_draft())
    bad.decisions = bad.decisions[:-3]
    llm.dv_intents = [bad]
    with pytest.raises(Exception, match="NOT_RUN|nothing was simulated"):
        run_tb(engine)
    assert [c.name for c in llm.calls][:2] == ["tb_intent", "tb_intent"] and "no decision for" in llm.calls[1].prompt


def test_blocked_issue_stops_before_freezing(engine, llm):
    i = good_intent(example_draft())
    i.open_issues.append(C.OpenIssue(id="OI-002", kind="BLOCKED", requirement_ids=["REQ-042"], question="What do the outputs read after reset?",
                                     source="spec/spec.md:192"))
    llm.dv_intents = [i]
    run_tb(engine)  # no failure: the step waits for the answer
    c = load_contract(engine.project.root)
    assert c.metadata.status == "draft" and not (engine.project.root / "schemas" / "tb_architecture.json").exists()
    qs = json.loads((engine.project.root / "schemas" / "questions" / "testplan.json").read_text())
    assert any(q["blocking"] for q in qs)


# -- stable question IDs, review ledger, answered issues ------------------------------------------------------------

def test_issue_questions_have_stable_ids(engine, llm):
    with pytest.raises(Exception, match="NOT_RUN|nothing was simulated"):
        run_tb(engine)
    qs = json.loads((engine.project.root / "schemas" / "questions" / "testplan.json").read_text())
    assert [q["id"] for q in qs] == ["Q-TB-OI-001"]


def test_candidate_decided_normative_needs_a_reason_and_no_open_issue():
    d = example_draft()
    i = good_intent(d)
    for x in i.decisions:
        if x.req_id == "REQ-042":
            x.origin, x.status, x.status_reason = "NORMATIVE", "planned", ""
    c, problems = apply_intent(d, i)
    assert any("REQ-042" in p and "give the reason" in p for p in problems) and any("REQ-042" in p and "unresolved open issue" in p for p in problems)


def test_an_answered_issue_must_be_applied():
    d = example_draft()
    _, problems = apply_intent(d, good_intent(d), answered={"OI-001"})
    assert any("OI-001" in p and "apply the answer" in p for p in problems)
    i = good_intent(d)
    i.open_issues[0].resolved, i.open_issues[0].resolution = True, "approved: add the test"
    for x in i.decisions:
        if x.req_id in i.open_issues[0].requirement_ids:
            x.origin, x.status, x.status_reason = "NORMATIVE", "planned", "approved by the user's answer Q-TB-OI-001"
            i.objectives[0].requirement_ids.append(x.req_id)
    c, problems = apply_intent(d, i, answered={"OI-001"})
    assert problems == [], problems


def test_review_ledger_separates_who_decided(tmp_path):
    from q3tui.flows.dv.uvm import review

    d = example_draft()
    c, _ = apply_intent(d, good_intent(d))
    c = c.model_copy(update={"open_issues": [c.open_issues[0].model_copy(update={"id": f"OI-00{n}"}) for n in (1, 2, 3, 4)]})
    answers = {"Q-TB-OI-001": "yes, golden"}
    decisions = {"Q-TB-OI-002": {"decision": "accept_default", "by": "me", "at": "t", "note": "ok", "issue_sha": review.issue_sha(c.open_issues[1])},
                 "Q-TB-OI-004": {"decision": "accept_default", "by": "me", "at": "t", "note": "ok", "issue_sha": "stale"}}
    rows = review.ledger(c, answers, decisions, {"Q-TB-OI-003": "the default"})
    assert [r["class"] for r in rows] == ["user_answered", "user_accepted_default", "auto_default", "pending"]
    assert "changed after your review" in rows[3]["note"]  # a decision bound to other text no longer counts
    assert rows[0]["answer"] == "yes, golden" and rows[1]["by"] == "me" and rows[2]["default"] == "the default"
    with pytest.raises(ValueError):
        review.record_decision(SimpleNamespace(schemas_dir=tmp_path), c.open_issues[0], "approve", "me", "t")


def intent_from_contract(c: C.VerificationContract) -> DvIntent:
    """The intent that reproduces a contract (what a model returns when it keeps everything and only applies the new answers)."""
    return DvIntent(
        decisions=[ReqDecision(req_id=r.req_id, observability=r.observability, observability_reason=r.observability_reason,
                               origin=r.origin_class if r.origin_class in ("NORMATIVE", "ASSUMED") else None, status=r.status, status_reason=r.status_reason)
                   for r in c.requirements],
        interfaces=c.interface_contract.interfaces, objectives=c.objectives, scoreboard_rules=c.scoreboard_rules, coverage=c.coverage,
        tests=c.tests, open_issues=c.open_issues)


def test_example_baseline_review_report(engine, llm):
    """The committed baseline (the contract the user reviewed) with the user's recorded answer and decisions."""
    root = engine.project.root
    for n in ("golden_manifest", "verification_contract", "tb_architecture"):
        shutil.copy(EXAMPLE / "schemas" / f"{n}.json", root / "schemas")
    shutil.copytree(EXAMPLE / "schemas" / "questions", root / "schemas" / "questions")
    shutil.copy(EXAMPLE / "schemas" / "dv_review_decisions.json", root / "schemas")
    base = load_contract(root)
    assert C.frozen_ok(base) and [o.id for o in base.open_issues] == [f"OI-00{n}" for n in range(1, 7)]
    i = intent_from_contract(base)
    # the model returns everything as it was and tries to reword a reviewed issue on the way: the wording is restored
    i.open_issues = [o.model_copy(update={"question": "reworded"}) if o.id == "OI-001" else o for o in i.open_issues]
    llm.dv_intents = [i]
    with pytest.raises(Exception, match="NOT_RUN|nothing was simulated"):
        run_tb(engine)
    rep = json.loads((root / "schemas" / "dv_review.json").read_text())
    classes = {r["issue_id"]: r["class"] for r in rep["issues"]}
    assert classes == {"OI-001": "user_accepted_default", "OI-002": "user_accepted_default", "OI-003": "user_accepted_default",
                       "OI-004": "user_accepted_default", "OI-005": "user_accepted_default", "OI-006": "user_answered"}
    md = (root / "reports" / "dv_review_report.md").read_text()
    assert "Answered by you" in md and "TC-012" in md and "thaphan@nvidia.com" in md and "reworded" not in md
    assert "keeps incrementing (modulo 2**PR_ADDR_WD" in md  # the user's recorded answer keeps its original wording
    assert "keeps incrementing modulo 2**PARA_ADDR_WD" in md  # the contract uses the renamed parameter


# -- where an open issue's answer goes: the spec step, or nowhere (a testbench choice); and no question asked twice ----------------------------------------

def _issue(oid, question, scope):
    return C.OpenIssue(id=oid, kind="OPEN", requirement_ids=["REQ-001"], question=question, source="spec.md:1", default_assumption="the default", scope=scope)


def test_a_testbench_choice_stays_in_the_test_plan_and_a_spec_issue_goes_to_the_spec(tmp_path):
    from q3tui.flows.dv.uvm.intent import issue_questions

    d = example_draft()
    c = d.model_copy(update={"open_issues": [
        _issue("OI-001", "How many random vectors should the reduced-configuration random tests run?", "verification"),
        _issue("OI-002", "How soon after asynchronous assertion of reset must o_busy and o_done be low?", "spec"),
        C.OpenIssue(id="OI-003", kind="OPEN", question="an issue from a contract written before scopes existed", default_assumption="x")]})
    assert [(q.id, q.kind) for q in issue_questions(c)] == [("Q-TB-OI-001", "design_choice"), ("Q-TB-OI-002", "spec_gap"), ("Q-TB-OI-003", "spec_gap")]  # (the default: the spec)
    # the engine sends only a spec_gap back to the spec step: the testbench choice stays with the step that asked
    from q3tui.core.project import Project
    from q3tui.pipeline.engine import Engine

    (tmp_path / "q3tui.yaml").write_text("pipeline: {flow: dv}\n")
    eng = Engine(Project.open(tmp_path))
    assert eng.gap_target("testplan", {"kind": "spec_gap"}) == "spec" and eng.gap_target("testplan", {"kind": "design_choice"}) is None
    assert eng.gap_target("testplan", {}) is None  # (no kind: a testplan question is about its own work)


def test_an_updated_contract_keeps_the_scope_the_user_reviewed():
    d = example_draft()
    first = d.model_copy(update={"open_issues": [_issue("OI-001", "How many random vectors?", "verification")]})
    i = good_intent(d)
    i.open_issues = [_issue("OI-001", "How many random vectors?", "spec")]  # the model changes its mind on a later run
    c, _ = apply_intent(d, i, previous=first)
    assert c.open_issues[0].scope == "verification"


def test_a_question_that_only_repeats_an_open_issue_is_not_asked_twice():
    from q3tui.flows.dv.uvm.intent import drop_repeats, issue_questions
    from q3tui.steps.common import Question

    d = example_draft()
    c = d.model_copy(update={"open_issues": [
        _issue("OI-001", "How many random vectors should the reduced-configuration random tests (CFG-MIN and CFG-MAX) run?", "verification"),
        _issue("OI-003", "TEST-012 is a formal proof (K=2, W=2..3). Is the UVM testbench expected to run it?", "verification")]})
    issues = issue_questions(c)
    own = [Question(id="", question="How many random vectors should the reduced-configuration random tests (CFG-MIN, CFG-MAX) use?", blocking=False, default_assumption="1000"),
           Question(id="", question="TEST-012 is a formal proof. Should this UVM contract include only a simulation check of it?", blocking=False, default_assumption="yes"),
           Question(id="", question="Which clock edge samples i_start?", blocking=False, default_assumption="rising")]
    kept, dropped = drop_repeats(own, issues)
    assert [q.question for q in kept] == ["Which clock edge samples i_start?"] and len(dropped) == 2


def test_the_review_report_marks_a_testbench_choice():
    from q3tui.flows.dv.uvm import review

    d = example_draft()
    c = d.model_copy(update={"open_issues": [_issue("OI-001", "How many random vectors?", "verification"), _issue("OI-002", "Reset timing?", "spec")]})
    rows = review.ledger(c, {}, {}, {})
    assert [r["scope"] for r in rows] == ["verification", "spec"]
    md = review.render({"summary": {k: 0 for k in review.CLASSES} | {"pending": 2}, "issues": rows, "contract_hash": "h", "contract_status": "draft",
                        "requirements": {s: 0 for s in ("planned", "open", "blocked", "not_observable", "excluded")}, "assumption_candidates_decided_normative": []}, c)
    assert md.count("a testbench choice: it stays in the test plan") == 1


def test_a_contract_frozen_before_issues_had_a_scope_keeps_its_hash():
    d = example_draft()
    plain = d.model_copy(update={"open_issues": [C.OpenIssue(id="OI-001", kind="OPEN", question="q", default_assumption="x")]})
    spec = d.model_copy(update={"open_issues": [plain.open_issues[0].model_copy(update={"scope": "spec"})]})
    verification = d.model_copy(update={"open_issues": [plain.open_issues[0].model_copy(update={"scope": "verification"})]})
    assert C.contract_hash(plain) == C.contract_hash(spec) != C.contract_hash(verification)


def test_a_change_request_can_reclassify_an_issue_but_a_plain_update_cannot():
    d = example_draft()
    first = d.model_copy(update={"open_issues": [_issue("OI-001", "Reset timing?", "verification")]})

    def intent():  # (apply_intent rewrites the intent's issues: a fresh one per call)
        i = good_intent(d)
        i.open_issues = [_issue("OI-001", "Reset timing?", "spec")]
        return i

    assert apply_intent(d, intent(), previous=first)[0].open_issues[0].scope == "verification"  # an update keeps what you saw
    assert apply_intent(d, intent(), previous=first, keep_scope=False)[0].open_issues[0].scope == "spec"  # a change request asked for it
