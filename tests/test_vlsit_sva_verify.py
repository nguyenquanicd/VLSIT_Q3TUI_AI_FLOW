"""VLSIT steps `sva` and `verify`: SVA files, RTM, review API, simulation, mutation, fix loop (no live LLM, fake tools)."""

import json
import sys
import textwrap

import anyio
import pytest

from q3tui.llm import runtime
from q3tui.llm.runtime import StageResult
from q3tui.pipeline.engine import Engine, EngineError
from q3tui.core.project import Project
from q3tui.steps.vlsit import artifacts, mutation, rtm, simlog
from q3tui.flows.vlsit.sva import review
from q3tui.flows.vlsit.sva.step import SvaFile, check_content
from q3tui.flows.vlsit.verify.step import TcVerdict, VerifyTriage
from tests.fakes import FakeLLM
from tests.vlsit_fakes import register

SVA = {
    "toy_alu": textwrap.dedent("""\
        `default_nettype none
        module toy_alu_sva (input logic i_clk, input logic i_resetn, input logic [3:0] i_a, input logic [3:0] i_b);
        `ifndef SYNTHESIS
          // NL: the sum output equals a plus b
          // REQ: REQ-001
          a_sum : assert property (@(posedge i_clk) disable iff (!i_resetn) 1'b1) else $error("a_sum");
          // NL: the sum check is exercised
          // REQ: REQ-001
          c_sum_trig : cover property (@(posedge i_clk) disable iff (!i_resetn) i_a != 0) $display("COVER_HIT c_sum_trig");
        `endif
        endmodule
        `default_nettype wire
        """),
    "toy_cmp": textwrap.dedent("""\
        `default_nettype none
        module toy_cmp_sva (input logic i_clk, input logic i_resetn);
        `ifndef SYNTHESIS
          // NL: equal inputs raise the flag
          // REQ: REQ-002
          a_eq : assert property (@(posedge i_clk) disable iff (!i_resetn) 1'b1) else $error("a_eq");
          // NL: the equality trigger
          // REQ: REQ-002
          c_eq_trig : cover property (@(posedge i_clk) disable iff (!i_resetn) 1'b1) $display("COVER_HIT c_eq_trig");
        `endif
        endmodule
        `default_nettype wire
        """),
}
TRIAGE = {"verdicts": []}
INVESTIGATE = {"verdicts": []}


@register
def _handler(fake, stage, emit):
    if stage.name.startswith(("sva_", "sva_fix_")):
        module = stage.name.split("_", 2)[-1] if stage.name.startswith("sva_fix_") else stage.name[4:]
        return StageResult("", SvaFile(content=SVA[module], summary=""), 0.0, 1, "s")
    if stage.name == "verify_triage":
        return StageResult("", VerifyTriage.model_validate(TRIAGE), 0.0, 1, "s")
    if stage.name == "verify_investigate":
        return StageResult("", VerifyTriage.model_validate(INVESTIGATE), 0.0, 1, "s")
    return None


FAKE_SIM = """
import sys, pathlib
fl, work = sys.argv[1], pathlib.Path(sys.argv[2])
text = ""
bad = False
for line in pathlib.Path(fl).read_text().splitlines():
    if line.startswith("+"):
        continue
    p = pathlib.Path(line)
    t = p.read_text()
    if "SYNTAXERR" in t:
        print(f"{p}:1: error: syntax error"); bad = True
    if "_sva" not in p.name and "_bind" not in p.name and "/tb/" not in str(p):
        text += t
(work / "model.txt").write_text(text)
sys.exit(1 if bad else 0)
"""
FAKE_RUN = """
import sys, pathlib
work, tc = pathlib.Path(sys.argv[1]), sys.argv[2]
t = (work / "model.txt").read_text()
ok = {"TC-001": " i_a + i_b" in t, "TC-002": "i_a == i_b" in t}.get(tc, True)
if tc == "TC-001":
    print("COVER_HIT c_sum_trig")
if "VIOL" in t:
    print("Error: a_sum failed at 10ns")
print(f"TC_RESULT {tc} {'PASS' if ok else 'FAIL'}")
print("CYCLES=42")
"""


@pytest.fixture
def project(tmp_path):
    (tmp_path / "fake_sim.py").write_text(FAKE_SIM)
    (tmp_path / "fake_run.py").write_text(FAKE_RUN)
    py = sys.executable
    (tmp_path / "q3tui.yaml").write_text(textwrap.dedent(f"""\
        pipeline: {{flow: vlsit, max_fix_iterations: 2, escalate_after_repeats: 2}}
        tools:
          roles:
            sim: {{cmd: "{py} {tmp_path}/fake_sim.py {{filelist}} {{workdir}}", parse: iverilog, supports: [sva]}}
            run: {{cmd: "{py} {tmp_path}/fake_run.py {{workdir}} {{tc}}", parse: iverilog}}
        """))
    p = Project.open(tmp_path)
    write = lambda name, data: artifacts.write(p, name, data, check=False)  # noqa: E731
    write("structured_spec.json", {
        "metadata": {"ip_name": "toy", "spec_revision": "1", "phase": 1, "gate_status": "approved"},
        "requirements": [
            {"req_id": "REQ-001", "text": "o = a + b", "category": "functional", "ambiguity_score": 0.1, "sva_hint": "property"},
            {"req_id": "REQ-002", "text": "flag when a == b", "category": "functional", "ambiguity_score": 0.1}],
        "parameters": [], "modules": [{"name": "toy_alu", "req_ids": ["REQ-001"]}, {"name": "toy_cmp", "req_ids": ["REQ-002"]}]})
    write("selected_testplan.json", {"metadata": {"ip_name": "toy", "gate_4_approved": True},
                                     "test_cases": [{"tc_id": "TC-001", "title": "add", "req_ids": ["REQ-001"], "selected": True},
                                                    {"tc_id": "TC-002", "title": "cmp", "req_ids": ["REQ-002"], "selected": True}]})
    rtl = p.src_dir / "rtl"
    rtl.mkdir(parents=True)
    (rtl / "toy_alu.sv").write_text("module toy_alu(input logic [3:0] i_a, i_b, output logic [4:0] o_s);\n"
                                    "  assign o_s = i_a + i_b; // REQ-001\nendmodule\n")
    (rtl / "toy_cmp.sv").write_text("module toy_cmp(input logic [3:0] i_a, i_b, output logic o_eq);\n"
                                    "  assign o_eq = (i_a == i_b); // REQ-002\nendmodule\n")
    tb = p.src_dir / "tb"
    tb.mkdir()
    (tb / "toy_tb.sv").write_text("module toy_tb; endmodule\n")
    return p


@pytest.fixture
def llm(monkeypatch):
    fake = FakeLLM()
    monkeypatch.setattr(runtime, "run_stage", fake)
    TRIAGE["verdicts"] = []
    return fake


def engine_of(project):
    return Engine(Project.open(project.root))


def run_step(engine, name):
    anyio.run(lambda: engine.run_step(engine.by_name[name]))


# -- pieces ---------------------------------------------------------------------------------------


def test_simlog():
    ok = simlog.parse_run("TC-1", "x\nTC_RESULT TC-1 PASS\nCYCLES=12\n", 0, False)
    assert (ok.status, ok.cycles) == ("pass", 12)
    assert simlog.parse_run("TC-1", "Error: a_x failed\nTC_RESULT TC-1 PASS", 0, False).status == "fail"
    assert simlog.parse_run("TC-1", "nothing here", 0, False).status == "fail"  # no marker: never a pass
    assert simlog.parse_run("TC-1", "", None, True).status == "timeout"
    assert simlog.parse_run("TC-1", "[FAIL]", 0, False).status == "fail"
    assert simlog.cover_hits("COVER_HIT c_a\nCOVER_HIT c_b") == {"c_a", "c_b"}


def test_check_and_parse_sva():
    assert check_content("toy_alu", SVA["toy_alu"], ["REQ-001"]) == []
    props = rtm.parse_sva_text(SVA["toy_alu"], "toy_alu")
    assert [(p["label"], p["kind"], p["req_ids"]) for p in props] == [("a_sum", "assert", ["REQ-001"]), ("c_sum_trig", "cover", ["REQ-001"])]
    assert props[0]["nl"] == "the sum output equals a plus b"
    bad = SVA["toy_alu"].replace("// NL: the sum output equals a plus b\n", "").replace("`ifndef SYNTHESIS", "")
    problems = check_content("toy_alu", bad, ["REQ-001", "REQ-009"])
    assert any("NL" in p for p in problems) and any("SYNTHESIS" in p for p in problems) and any("REQ-009" in p for p in problems)


def test_mutation_candidates():
    text = "module m(input a);\n  assign x = (a == b) && c; // REQ-001\n  assign y = a + b;\nendmodule\n"
    ops = {(n, old, new) for n, _, old, new in mutation.candidates(text)}
    assert (2, "==", "!=") in ops and (2, "&&", "||") in ops and (3, "+", "-") in ops
    m = mutation.Mutant("m#1", "m", "f.sv", 3, text.splitlines()[2].index("+"), "+", "-")
    assert "a - b" in mutation.apply(text, m)


# -- sva step -----------------------------------------------------------------------------------


def test_sva_step_writes_files_review_and_rtm(project, llm):
    eng = engine_of(project)
    run_step(eng, "sva")
    sva = project.src_dir / "sva"
    assert (sva / "toy_alu_sva.sv").is_file() and (sva / "toy_cmp_sva.sv").is_file()
    assert "bind toy_alu toy_alu_sva" in (sva / "toy_bind.sv").read_text()
    assert "toy_alu_sva.sv" in (sva / "filelist_sva.f").read_text()
    review_md = (sva / "GATE3_REVIEW.md").read_text()
    assert "compile check: **pass**" in review_md and "a_sum" in review_md
    doc = artifacts.read(project, "rtm.json")
    assert artifacts.validate("rtm.json", doc) == []
    assert all(not r["sva_traced"] for r in doc["requirements"])  # nothing confirmed yet
    props = {p["label"]: p for p in review.load_properties(eng)}
    assert props["a_sum"]["status"] == "pending"
    assert props["a_eq"]["vacuous"] is True and props["a_sum"]["vacuous"] is False  # the smoke run hit only c_sum_trig
    assert eng.evaluate(eng.by_name["sva"]).gate == "open"
    assert not [p for p in eng.evaluate(eng.by_name["sva"]).edited]


def test_sva_update_not_rerun_and_rejection(project, llm):
    eng = engine_of(project)
    run_step(eng, "sva")
    n = len(llm.calls)
    eng2 = engine_of(project)
    run_step(eng2, "sva")  # nothing changed: no LLM
    assert len(llm.calls) == n
    msg = review.review_property(eng2, "a_sum", "rejected", "it is a tautology")
    assert "sent back" in msg
    assert any(f.startswith("[sva:toy_alu]") for f in eng2.state.feedback["sva"])
    with pytest.raises(EngineError):
        review.review_property(eng2, "a_sum", "rejected", "")
    n = len(llm.calls)
    run_step(eng2, "sva")  # only the named module runs
    assert [c.name for c in llm.calls[n:]] == ["sva_toy_alu"]


def test_sva_compile_na_without_sva_support(project, llm):
    cfg = (project.root / "q3tui.yaml").read_text().replace(", supports: [sva]", "")
    (project.root / "q3tui.yaml").write_text(cfg)
    eng = engine_of(project)
    run_step(eng, "sva")
    text = (project.src_dir / "sva" / "GATE3_REVIEW.md").read_text()
    assert "compile check: **N/A**" in text and "vacuity: **N/A**" in text  # never reported as passed
    assert all(p["vacuous"] is None for p in review.load_properties(eng))


def test_review_confirm_all_and_sign_refusal(project, llm):
    eng = engine_of(project)
    run_step(eng, "sva")
    assert "confirmed 1 assertion" in review.confirm_all(eng)  # a_eq is vacuous: left pending
    rows = {r["req_id"]: r for r in review.rtm_rows(eng)}
    assert rows["REQ-001"]["sva_traced"] and not rows["REQ-002"]["sva_traced"]
    with pytest.raises(EngineError, match="cannot be signed off"):
        review.sign_req(eng, "REQ-001", True)  # not simulated, no mutation


def test_gate_3b_refuses_approval_while_assertions_are_pending(project, llm):
    """Gate 3b (human) does not just check blocking questions: pending_reviews() (review.pending_ids, vacuous
    excluded) refuses Approve until every non-vacuous assertion is confirmed or rejected."""
    eng = engine_of(project)
    run_step(eng, "sva")
    assert eng.by_name["sva"].pending_reviews(eng) == ["a_sum"]  # a_eq: vacuous, excluded
    with pytest.raises(EngineError, match="1 item.*not reviewed yet.*a_sum"):
        eng.approve("sva")
    eng.approve("sva", force=True)  # force still bypasses it, like a blocking question
    assert eng.state.gates["sva"].status == "approved"


def test_gate_3b_approves_once_pending_assertions_are_reviewed(project, llm):
    eng = engine_of(project)
    run_step(eng, "sva")
    review.review_property(eng, "a_sum", "confirmed")
    assert eng.by_name["sva"].pending_reviews(eng) == []
    eng.approve("sva")  # no force needed: nothing pending any more
    assert eng.state.gates["sva"].status == "approved"


def test_gate_3b_auto_confirm_reviews_setting_bypasses_the_check(project, llm):
    eng = engine_of(project)
    run_step(eng, "sva")
    eng.project.cfg.pipeline.auto_confirm_reviews = True
    assert eng.auto_confirm_reviews()
    eng.approve("sva")  # a_sum is still "pending" in review.py, but the gate no longer waits for it
    assert eng.state.gates["sva"].status == "approved"


# -- verify step ----------------------------------------------------------------------------------


def test_verify_end_to_end_signoff(project, llm):
    eng = engine_of(project)
    run_step(eng, "sva")
    review.confirm_all(eng, include_vacuous=True)
    assert eng.by_name["verify"].missing_input(eng) is None
    run_step(eng, "verify")
    report = artifacts.read(project, "verification_report.json")
    assert artifacts.validate("verification_report.json", report) == []
    assert report["sim_summary"]["pass"] == 2 and report["sim_summary"]["fail"] == 0
    assert report["sim_summary"]["tc_results"][0]["cycles"] == 42
    doc = artifacts.read(project, "rtm.json")
    assert artifacts.validate("rtm.json", doc) == []
    rows = {r["req_id"]: r for r in doc["requirements"]}
    assert rows["REQ-001"]["sim_pass"] and rows["REQ-001"]["mutation_score"] == 1.0
    assert rows["REQ-001"]["mutation_detail"]["killed"] == rows["REQ-001"]["mutation_detail"]["total"] >= 1
    assert all(not r["signed_off"] for r in rows.values())  # only you sign
    assert review.sign_req(eng, "REQ-001", True, "ok") == "REQ-001 signed off"
    assert artifacts.read(project, "rtm.json")["requirements"][0]["signed_off"] is True
    # Gate 5 approved ≠ everything signed off; the gate is recorded in both artifacts
    assert eng.evaluate(eng.by_name["verify"]).gate == "open"
    eng.approve("verify")
    doc = artifacts.read(project, "rtm.json")
    assert doc["metadata"]["gate_5_approved"] is True and doc["requirements"][1]["signed_off"] is False
    assert artifacts.read(project, "verification_report.json")["metadata"]["gate_5_approved"] is True
    assert eng.evaluate(eng.by_name["verify"]).edited == []
    # the signature is withdrawn by the files, not kept blindly: an RTL change invalidates the simulation
    (project.src_dir / "rtl" / "toy_alu.sv").write_text((project.src_dir / "rtl" / "toy_alu.sv").read_text() + "// touched\n")
    assert review.rtm_rows(eng)[0]["signed_off"] is False


def test_sign_all_signs_only_whats_ready(project, llm):
    eng = engine_of(project)
    run_step(eng, "sva")
    review.confirm_all(eng, include_vacuous=True)
    run_step(eng, "verify")
    rows = {r["req_id"]: r for r in review.rtm_rows(eng)}
    ready = [rid for rid, r in rows.items() if not r["signed_off"] and not rtm.lock_reasons(r)]
    assert ready  # REQ-001 at least
    msg = review.sign_all(eng, note="bulk ok")
    assert f"signed off {len(ready)} requirement(s)" in msg
    after = {r["req_id"]: r for r in review.rtm_rows(eng)}
    for rid in ready:
        assert after[rid]["signed_off"] is True and after[rid]["sign_off_note"] == "bulk ok"
    not_ready = [rid for rid in rows if rid not in ready]
    assert all(not after[rid]["signed_off"] for rid in not_ready)
    # a second call signs nothing new (already signed, or still not ready)
    assert review.sign_all(eng) == "signed off 0 requirement(s)" + (f"; {len(not_ready)} still locked (conditions 1-5 not met)" if not_ready else "")


def test_verify_reuses_results_when_unchanged(project, llm):
    eng = engine_of(project)
    run_step(eng, "sva")
    run_step(eng, "verify")
    before = (project.state_dir / "sim" / "verify" / "results.json").stat().st_mtime_ns
    run_step(engine_of(project), "verify")
    assert (project.state_dir / "sim" / "verify" / "results.json").stat().st_mtime_ns == before


def test_verify_fix_loop_dispatches_then_escalates(project, llm):
    eng = engine_of(project)
    run_step(eng, "sva")
    (project.src_dir / "rtl" / "toy_alu.sv").write_text("module toy_alu(input logic [3:0] i_a, i_b, output logic [4:0] o_s);\n"
                                                        "  assign o_s = i_a - i_b; // REQ-001\nendmodule\n")
    TRIAGE["verdicts"] = [{"tc_id": "TC-001", "verdict": "RTL_BUG", "unit": "toy_alu", "summary": "the output is a difference",
                           "fix": "The sum output must be a plus b, not a minus b."}]
    run_step(eng, "verify")
    assert any(f.startswith("[rtl:toy_alu]") for f in eng.state.feedback["rtl"])
    assert eng.state.step_meta["verify"]["loop"] is True and eng.state.step_meta["verify"]["fix_rounds"] == 1
    assert artifacts.read(project, "rtm.json")["requirements"][0]["mutation_score"] == "pending"  # no mutation on a failing baseline
    # the same finding again and again becomes a question, not another blind round
    run_step(eng, "verify")
    run_step(eng, "verify")
    qs = eng.by_name["verify"].question_list(eng)
    assert qs and qs[0]["id"].startswith("VF") and qs[0]["blocking"]  # (not something to approve with a default)
    # the original: "a TC FAILs → Gate 5 cannot be signed" — approving the gate never signs over a failing test case
    eng.state.gates["verify"] = __import__("q3tui.pipeline.state", fromlist=["GateRecord"]).GateRecord(status="open")
    eng.approve("verify", force=True)
    assert artifacts.read(project, "rtm.json")["metadata"]["gate_5_approved"] is False
    assert artifacts.read(project, "verification_report.json")["metadata"]["gate_5_approved"] is False


def test_verify_compile_errors_are_routed_by_file(project, llm):
    eng = engine_of(project)
    run_step(eng, "sva")
    (project.src_dir / "rtl" / "toy_cmp.sv").write_text("SYNTAXERR\n")
    run_step(eng, "verify")
    assert any(f.startswith("[rtl:toy_cmp]") for f in eng.state.feedback["rtl"])
    assert eng.state.step_meta["verify"]["loop"] is True
    report = artifacts.read(project, "verification_report.json")
    assert {r["status"] for r in report["sim_summary"]["tc_results"]} == {"skipped"}
    assert report["sim_summary"]["pass"] == 0


def test_verify_waits_for_the_tools(tmp_path):
    # (a partly configured tool set: with none at all the built-in Synopsys preset is used)
    (tmp_path / "q3tui.yaml").write_text("pipeline: {flow: vlsit}\ntools:\n  roles:\n    run: {cmd: 'echo {tc}'}\n")
    p = Project.open(tmp_path)
    artifacts.write(p, "structured_spec.json", {"metadata": {}, "requirements": []}, check=False)
    artifacts.write(p, "selected_testplan.json", {"metadata": {}, "test_cases": []}, check=False)
    (p.src_dir / "rtl").mkdir(parents=True)
    (p.src_dir / "rtl" / "a.sv").write_text("module a; endmodule\n")
    eng = Engine(p)
    assert "tool role 'sim' is not configured" in eng.by_name["verify"].missing_input(eng)
    assert eng.evaluate(eng.by_name["verify"]).status == "missing_input"


def test_a_failed_test_case_carries_the_message_of_its_failed_checks():
    from q3tui.steps.vlsit import simlog

    text = "COVER_HIT c_x\nTC_DETAIL TC-017 FAIL: wrong beat count after reset\nTC_DETAIL TC-017 FAIL: wrong beat count after reset\nTC_DETAIL TC-001 FAIL: other\nTC_RESULT TC-017 FAIL\nTC_SUMMARY total=1 failed=1\n"
    out = simlog.parse_run("TC-017", text, 0, False)
    assert out.status == "fail" and out.reason == "the testbench reported FAIL: wrong beat count after reset"  # only its own, once


def test_tool_versions_come_from_the_banners_of_the_real_logs(tmp_path):
    from types import SimpleNamespace

    from q3tui.steps.vlsit import simenv

    sim = tmp_path / ".q3tui" / "sim"
    (sim / "verify").mkdir(parents=True)
    (sim / "rtl").mkdir()
    (sim / "verify" / "compile.log").write_text("                         Chronologic VCS (TM)\n         Version X-2025.06_Full64 -- Thu Oct  1\n")
    (sim / "rtl" / "synth_tt.log").write_text("Design Compiler Graphical\nDC Ultra (TM)\n               Version V-2023.12-SP3 for linux64 - Apr 17, 2024\n")
    eng = SimpleNamespace(project=SimpleNamespace(state_dir=tmp_path / ".q3tui"))
    assert simenv.tool_versions(eng, None, ["sim", "run", "lint", "synth"]) == {
        "sim": "VCS X-2025.06_Full64", "run": "VCS X-2025.06_Full64", "synth": "Design Compiler V-2023.12-SP3"}  # lint: no log, no guess


def test_a_test_case_triage_is_unsure_about_is_investigated_with_tools(project, llm):
    """UNSURE is not the end: a second stage reads the full log, the test, the RTL and the SVA (copied out of .q3tui/)."""
    eng = engine_of(project)
    run_step(eng, "sva")
    (project.src_dir / "rtl" / "toy_alu.sv").write_text("module toy_alu(input logic [3:0] i_a, i_b, output logic [4:0] o_s);\n"
                                                        "  assign o_s = i_a - i_b; // REQ-001\nendmodule\n")
    TRIAGE["verdicts"] = [{"tc_id": "TC-001", "verdict": "UNSURE", "unit": "", "summary": "no evidence visible", "fix": ""}]
    INVESTIGATE["verdicts"] = [{"tc_id": "TC-001", "verdict": "RTL_BUG", "unit": "toy_alu", "summary": "o_s is a minus b",
                                "fix": "The sum output must be a plus b, not a minus b."}]
    try:
        run_step(eng, "verify")
        inv = [c for c in llm.calls if c.name == "verify_investigate"]
        assert len(inv) == 1 and set(inv[0].task_tools or inv[0].builtin_tools) == {"Read", "Grep", "Glob"}  # (read-only: it changes no file)
        copy = project.root / "sim" / "vlsit_verify" / "TC-001.log"
        assert copy.is_file() and "TC_RESULT TC-001 FAIL" in copy.read_text()  # a log the stage may read
        assert "sim/vlsit_verify/TC-001.log" in inv[0].prompt and "src/rtl/" in inv[0].prompt
        assert any(f.startswith("[rtl:toy_alu]") for f in eng.state.feedback["rtl"])  # the verdict was dispatched like any other
        assert not eng.by_name["verify"].question_list(eng)  # (no question: it was placed)
        # still unsure after reading: a (blocking) question for you, as before
        INVESTIGATE["verdicts"] = [{"tc_id": "TC-001", "verdict": "UNSURE", "unit": "", "summary": "read all, still ambiguous", "fix": ""}]
        eng.state.feedback["rtl"] = []
        eng.state.step_meta["verify"]["fix_rounds"] = 0
        run_step(eng, "verify")
        assert eng.by_name["verify"].question_list(eng)[0]["blocking"]
    finally:
        TRIAGE["verdicts"], INVESTIGATE["verdicts"] = [], []


def test_the_vlsit_flow_allows_more_fix_rounds_for_verify():
    from q3tui import flows

    spec = flows.load_flow("vlsit").spec("verify")
    assert spec.options == {"max_fix_rounds": 8, "investigate": True}


def test_a_mutants_sources_never_include_the_original_rtl_again(project, llm, tmp_path):
    """Real run: tb/filelist.f lists the RTL first, so a mutant's source list had the mutated AND the original module — the
    simulator used the original and all 29 mutants "survived" (score 0)."""
    from q3tui.steps.vlsit import simenv

    eng = engine_of(project)
    rtl, tb = project.src_dir / "rtl", project.src_dir / "tb"
    tb.mkdir(parents=True, exist_ok=True)
    (rtl / "toy_alu.sv").write_text("module toy_alu; endmodule\n")
    (tb / "tb_top.sv").write_text("module tb_top; endmodule\n")
    (tb / "filelist.f").write_text("src/rtl/toy_alu.sv\n+incdir+src/tb\nsrc/tb/tb_top.sv\n")
    mut = tmp_path / "mutant" / "rtl"
    mut.mkdir(parents=True)
    (mut / "toy_alu.sv").write_text("module toy_alu; /* mutated */ endmodule\n")
    files, _ = simenv.sources(eng, False, rtl_dir=mut)
    assert [p.name for p in files] == ["toy_alu.sv", "tb_top.sv"] and files[0].parent == mut  # the mutant's copy, once
    plain, _ = simenv.sources(eng, False)
    assert [p.name for p in plain].count("toy_alu.sv") == 1  # and the normal list has it once


def test_gate_flags_in_the_rtm_survive_a_reset_of_the_file(project, llm):
    """Real run: `reset verify` deleted rtm.json (shared with sva), and the rebuilt RTM said gate_3_approved: false."""
    from q3tui.pipeline.state import GateRecord

    eng = engine_of(project)
    run_step(eng, "sva")
    eng.state.gates["sva"] = GateRecord(status="approved", approved_at="2026-01-01T00:00:00")
    (project.schemas_dir / "rtm.json").unlink()
    run_step(eng, "verify")
    md = artifacts.read(project, "rtm.json")["metadata"]
    assert md["gate_3_approved"] is True and md["gate_3_at"] == "2026-01-01T00:00:00"


def test_auto_confirm_is_opt_in_and_says_so(project, llm):
    eng = engine_of(project)
    run_step(eng, "sva")
    assert all(p["status"] == "pending" for p in review.load_properties(eng))  # by default nobody confirms for you
    eng.by_name["sva"].options = {"auto_confirm": True}
    eng.reset("sva", only=True)
    run_step(eng, "sva")
    props = review.load_properties(eng)
    assert any(p["status"] == "confirmed" and "auto-confirmed" in p["note"] for p in props)
    assert all(p["status"] == "confirmed" for p in props if not p["vacuous"] and p["kind"] == "assert")
    assert all(r["signed_off"] is False for r in review.rtm_rows(eng))  # requirements are still signed by you only


def test_pipeline_step_options_override_the_flow_files(tmp_path):
    from q3tui.pipeline.engine import Engine
    from q3tui.core.project import Project

    (tmp_path / "q3tui.yaml").write_text("pipeline:\n  flow: vlsit\n  step_options: {sva: {auto_confirm: true}, verify: {max_fix_rounds: 12}}\n")
    eng = Engine(Project.open(tmp_path))
    assert eng.by_name["sva"].options == {"auto_confirm": True}
    assert eng.by_name["verify"].options == {"max_fix_rounds": 12, "investigate": True}  # merged over the flow file's


def test_the_sign_command_signs_only_what_is_ready(project, llm, tmp_path):
    from click.testing import CliRunner

    from q3tui.core.cli import main

    eng = engine_of(project)
    run_step(eng, "sva")
    review.confirm_all(eng)
    run_step(eng, "verify")
    runner = CliRunner()
    out = runner.invoke(main, ["-C", str(project.root), "sign", "--all-ready", "-m", "ok"])
    rows = {r["req_id"]: r for r in review.rtm_rows(eng)}
    signed = [k for k, r in rows.items() if r["signed_off"]]
    assert signed and all(rows[k]["sign_off_note"] == "ok" for k in signed), out.output
    bad = runner.invoke(main, ["-C", str(project.root), "sign", "REQ-999"])  # unknown / not ready: refused, exit code says so
    assert bad.exit_code == 1


def test_auto_approve_confirms_the_assertions_and_signs_the_ready_requirements(project, llm):
    """auto approve = approve the gates, confirm the assertions, sign what is ready (never over a failing test)."""
    eng = engine_of(project)
    eng.project.cfg.pipeline.auto_approve = True
    assert eng.auto_reviews()
    run_step(eng, "sva")
    assert all(p["status"] == "confirmed" for p in review.load_properties(eng) if not p["vacuous"] and p["kind"] == "assert")
    run_step(eng, "verify")
    rows = review.rtm_rows(eng)
    ready_or_signed = [r for r in rows if r["status"] in ("signed_off", "pending")]
    assert ready_or_signed and all(r["status"] == "signed_off" and "auto-signed" in r["sign_off_note"] for r in ready_or_signed)
    assert all(r["signed_off"] is False for r in rows if r["status"] == "locked")  # a locked one is never signed
    # a failing test case: nothing is signed
    eng2 = engine_of(project)
    eng2.project.cfg.pipeline.auto_approve = True
    (project.src_dir / "rtl" / "toy_alu.sv").write_text("module toy_alu(input logic [3:0] i_a, i_b, output logic [4:0] o_s);\n"
                                                        "  assign o_s = i_a - i_b; // REQ-001\nendmodule\n")
    TRIAGE["verdicts"] = [{"tc_id": "TC-001", "verdict": "RTL_BUG", "unit": "toy_alu", "summary": "a minus b",
                           "fix": "The sum output must be a plus b, not a minus b."}]
    try:
        run_step(eng2, "verify")
    finally:
        TRIAGE["verdicts"] = []
    assert not any(r["signed_off"] for r in review.rtm_rows(eng2) if "TC-001" in (r.get("tc_ids") or []))


def test_the_step_options_override_auto_approve(project, llm):
    eng = engine_of(project)
    eng.project.cfg.pipeline.auto_approve = True
    eng.by_name["sva"].options = {"auto_confirm": False}
    run_step(eng, "sva")
    assert all(p["status"] == "pending" for p in review.load_properties(eng))  # (the step option wins)


def test_a_bind_file_error_goes_to_the_assertion_module_it_instantiates(project, llm):
    """Real run: `axi_downscaler_bind.sv:4 … 37-bit expression connected to 41-bit port i_wr_data of module X_sva` was sent to a
    "module" called axi_downscaler_bind (none): the fix request was ignored and the compile error stayed."""
    eng = engine_of(project)
    sva = project.src_dir / "sva"
    sva.mkdir(parents=True, exist_ok=True)
    (sva / "toy_alu_sva.sv").write_text("module toy_alu_sva (input logic i_clk);\nendmodule\n")
    (sva / "toy_alu_bind.sv").write_text("bind toy_alu toy_alu_sva u(.i_clk(i_clk));\n")
    msg = f'{sva / "toy_alu_bind.sv"}:4 Implicit port connection width mismatch — "toy_alu_sva u (.i_clk(i_clk));" connected to port of module "toy_alu_sva"'
    assert eng.by_name["verify"]._owner(eng, str(sva / "toy_alu_bind.sv"), msg) == ("sva", "toy_alu")
    assert eng.by_name["verify"]._owner(eng, str(sva / "toy_alu_bind.sv"), "no module named") == ("sva", "toy_alu_bind")  # (as before)


def test_approving_verify_without_any_simulation_results_does_not_crash(project, llm):
    """Real run: a compile failure left no results; approving the gate raised AttributeError ('NoneType' … 'get')."""
    from q3tui.pipeline.state import GateRecord

    eng = engine_of(project)
    run_step(eng, "sva")
    (project.src_dir / "rtl" / "toy_alu.sv").write_text("module toy_alu(input SYNTAXERR);\nendmodule\n")
    run_step(eng, "verify")
    eng.state.gates["verify"] = GateRecord(status="open")
    eng.approve("verify", force=True)
    assert artifacts.read(project, "rtm.json")["metadata"]["gate_5_approved"] is False  # a failed compile never signs Gate 5


def test_the_same_assertion_label_in_two_modules_is_two_assertions(project, llm):
    """Real run (rv32im): rv32im_div_sva and rv32im_muldiv_sva both define a_div_load_busy …; the TUI crashed with DuplicateKey and one
    review would have covered both."""
    eng = engine_of(project)
    sva = project.src_dir / "sva"
    sva.mkdir(parents=True, exist_ok=True)
    body = "module {m}_sva (input logic i_clk);\n  // NL: busy while loading\n  // REQ: REQ-001\n  a_div_load_busy : assert property (@(posedge i_clk) 1);\nendmodule\n"
    (sva / "div_sva.sv").write_text(body.format(m="div"))
    (sva / "muldiv_sva.sv").write_text(body.format(m="muldiv"))
    props = review.load_properties(eng)
    assert sorted(p["id"] for p in props) == ["div:a_div_load_busy", "muldiv:a_div_load_busy"]
    with pytest.raises(Exception, match="several modules"):
        review.review_property(eng, "a_div_load_busy", "confirmed")  # a bare label is ambiguous
    review.review_property(eng, "div:a_div_load_busy", "confirmed")
    status = {p["id"]: p["status"] for p in review.load_properties(eng)}
    assert status == {"div:a_div_load_busy": "confirmed", "muldiv:a_div_load_busy": "pending"}  # one review, one assertion
