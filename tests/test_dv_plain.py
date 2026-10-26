"""The dv flow with the plain SystemVerilog testbench (flows/dv/plain/, the steps testplan, tb, simulate): fake LLM stages, fake tools (scripts standing in
for Verilator / Icarus / Yosys), no live LLM and no EDA tool."""

import json
import re
import sys
import textwrap
from pathlib import Path

import anyio
import pytest

from q3tui.llm import runtime
from q3tui.llm.runtime import StageResult
from q3tui.pipeline.base import StepFailed
from q3tui.pipeline.engine import Engine
from q3tui.core.project import Project
from q3tui.flows.spec.lib import artifacts
from q3tui.flows.rtl.lib import rules_check, synth
from q3tui.flows.dv.plain import gen as tb_gen
from q3tui.flows.dv.plain import plan as tb_plan
from q3tui.flows.rtl.rtl.plan import RtlPlan, PlanModule, levels, plan_from_spec
from q3tui.flows.rtl.rtl.gen import VModuleResult
from q3tui.flows.dv.plain.plan import TbCase, TbExcluded, TbPlan, TbUnitResult
from tests.fakes import FakeLLM
from tests.vlsit_fakes import register
from q3tui.steps import register_kind

register_kind("rtl_gen", "q3tui.flows.rtl.rtl.gen:RtlGenStep")

# -- fake tools -----------------------------------------------------------------------------------------------

LINT = '''
import sys
files = [l.strip() for l in open(sys.argv[1]) if l.strip()]
for f in files:
    text = open(f).read()
    if "LINT_WARN" in text:
        print(f"%Warning-UNUSED: {f}:3:1: Signal is not used: 'x'")
    if "LINT_ERR" in text:
        print(f"%Error: {f}:4:1: syntax error")
        sys.exit(1)
'''
SIM = '''
import glob, sys
files = []
for l in open(sys.argv[1]):
    l = l.strip()
    if l.startswith("+incdir+"):  # the test cases are `included from tb_top: look at them like the compiler does
        files += sorted(glob.glob(l[len("+incdir+"):] + "/tests/*.sv"))
    elif l and not l.startswith("+"):
        files.append(l)
for l in files:
    text = open(l).read()
    if "SYNTAX_ERR" in text:
        print(f"{l}:3: error: syntax error")
        sys.exit(1)
    if "RTL_ERR" in text:
        print(f"{l}:2: error: port i_x not found")
        sys.exit(1)
'''
SYNTH = '''
import re, sys
script = open(sys.argv[1]).read()
top = re.search(r"-top (\\w+)", script).group(1)
print("Yosys 0.58 (git sha1 abc)")
for f in re.findall(r"^read_verilog -sv (\\S+)", script, re.M):
    if "SYNTH_ERR" in open(f).read():
        print(f"ERROR: {f}:2: Unsupported construct")
        sys.exit(1)
print("   Number of cells:                 42")
print(f"   Chip area for module '\\\\{top}': 1234.5")
'''


@pytest.fixture
def tools_dir(tmp_path):
    d = tmp_path / "faketools"
    d.mkdir()
    (d / "lint.py").write_text(LINT)
    (d / "sim.py").write_text(SIM)
    (d / "synth.py").write_text(SYNTH)
    (d / "run.py").write_text(RUN)
    return d


RUN = """import sys
tc, mode = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "pass")
if mode == "nores":
    print("simulation ended")
elif mode == "fail" or (mode == "failtc2" and tc == "TC-002"):
    print(f"TC_DETAIL {tc} FAIL: the checked value was wrong"); print(f"TC_RESULT {tc} FAIL")
else:
    print(f"TC_RESULT {tc} PASS")
"""


def tools_yaml(d: Path, synth_role=True, lint_role=True, run_mode="pass") -> str:
    py = sys.executable
    roles = [f"    run: {{cmd: '{py} {d}/run.py {{tc}} {run_mode}', parse: generic}}"]
    if lint_role:
        roles.append(f"    lint: {{cmd: '{py} {d}/lint.py {{filelist}} {{top}}', parse: verilator}}")
    roles.append(f"    sim: {{cmd: '{py} {d}/sim.py {{filelist}}', parse: iverilog}}")
    if synth_role:
        roles.append(f"    synth: {{cmd: '{py} {d}/synth.py {{script}}', parse: yosys}}")
    return "tools:\n  roles:\n" + "\n".join(roles) + "\n"


# -- the fake LLM's VLSIT stages -------------------------------------------------------------------------------

GOOD_RTL = """`default_nettype none
// Module : {name}
// REQ-IDs: {reqs}
module {name} #(
  parameter int unsigned PR_W = 8
) (
  input  logic            i_clk_core,
  input  logic            i_resetn_core,
  input  logic [PR_W-1:0] i_a,
  output logic [PR_W-1:0] o_y
);
  logic [PR_W-1:0] reg_q;
{insts}
  always_ff @(posedge i_clk_core or negedge i_resetn_core) begin : p_q
    if (!i_resetn_core) reg_q <= '0;
    else                reg_q <= i_a;
  end // {tags}
  assign o_y = reg_q;{extra}
endmodule
`default_nettype wire
"""


def _rtl_text(fake, name: str, bad: str | None) -> str:
    info = fake.vlsit_modules[name]
    insts = "".join(
        f"  {c} #(.PR_W(PR_W)) u_{c} (.i_clk_core(i_clk_core), .i_resetn_core(i_resetn_core), .i_a(i_a), .o_y());\n"
        for c in info["children"])
    extra = {"rule": "\n  initial begin end", "lint": "\n  // LINT_WARN", "synth": "\n  // SYNTH_ERR", None: ""}[bad]
    return GOOD_RTL.format(name=name, reqs=", ".join(info["reqs"]), insts=insts, tags=", ".join(info["reqs"]), extra=extra)


def _tc_text(path: Path, bad: bool) -> str:
    m = re.match(r"tc_(\d+)_(\w+)\.sv", path.name)
    tid, task = f"TC-{m.group(1)}", f"tc_{m.group(1)}_{m.group(2)}"
    return f"// {tid} | REQ-001\ntask automatic {task}();\n  `TC_CHECK(1'b1, \"ok\")\n{'  SYNTAX_ERR' if bad else ''}endtask\n"


TB_TOP = """// tb_top
module tb_top;
  `include "tb_common.svh"
  `include "tb_tests.svh"
  initial begin
    `include "tb_run.svh"
    `TC_SUMMARY;
    $finish;
  end
endmodule
"""


@register
def vlsit_handler(fake, stage, emit):
    n = stage.name
    if not hasattr(fake, "vlsit_modules"):
        return None
    if n == "rtl_plan":
        fake.vlsit_plan_calls += 1
        return StageResult("", RtlPlan(top="top", modules=[
            PlanModule(name=k, description=k, req_ids=v["reqs"], instances=v["children"]) for k, v in fake.vlsit_modules.items()]), 0.0, 1, "s")
    if n.startswith("rtl_"):
        name = n[4:]
        bad = None
        if fake.vlsit_bad.get(name):
            bad_kind = fake.vlsit_bad[name][0]
            fake.vlsit_bad[name] = fake.vlsit_bad[name][1:] if len(fake.vlsit_bad[name]) > 1 else []
            bad = bad_kind
        if fake.vlsit_always_bad.get(name):
            bad = fake.vlsit_always_bad[name]
        stage.write_files[0].write_text(_rtl_text(fake, name, bad))
        return StageResult("", VModuleResult(summary=f"{name} done", requirements=fake.vlsit_modules[name]["reqs"],
                                             questions=fake.vlsit_questions.pop(name, [])), 0.01, 2, f"sess-{name}")
    if n == "dv_triage":
        fake.vlsit_triage_calls += 1
        if fake.vlsit_triage_verdicts is None:
            raise RuntimeError("no credentials")  # (the triage cannot run: the step reports the failure as it was)
        from q3tui.flows.dv.triage import DvTriage
        from q3tui.flows.rtl.lib.models import Verdict

        return StageResult("", DvTriage(verdicts=[Verdict(**v) for v in fake.vlsit_triage_verdicts]), 0.0, 1, "s")
    if n == "tb_plan":
        fake.vlsit_tb_plan_calls += 1
        cases = fake.vlsit_tb_plans.pop(0) if len(fake.vlsit_tb_plans) > 1 else fake.vlsit_tb_plans[0]
        return StageResult("", TbPlan(test_cases=[TbCase(**c) for c in cases], excluded=[TbExcluded(**e) for e in getattr(fake, "vlsit_tb_excluded", [])]), 0.0, 1, "s")
    if n in ("tb_top", "tb_top_fix"):
        stage.write_files[0].write_text(TB_TOP if not fake.vlsit_top_bad else "module tb_top; endmodule\n")
        if n == "tb_top_fix" or fake.vlsit_top_bad:
            fake.vlsit_top_bad = max(0, fake.vlsit_top_bad - 1) if n == "tb_top" else fake.vlsit_top_bad
        if n == "tb_top_fix":
            stage.write_files[0].write_text(TB_TOP)
        return StageResult("", TbUnitResult(summary="top"), 0.0, 1, "s")
    if n.startswith("tb_"):  # tb_g<k>, tb_fix_<stem>, tb_fix_g<k>
        for p in [f for f in stage.write_files if f.name != "NOTES.md"]:  # (a role session adds its notes file)
            bad = fake.vlsit_tc_bad.get(p.name, 0) > 0 and not n.startswith("tb_fix")
            if bad or fake.vlsit_tc_always_bad.get(p.name):
                fake.vlsit_tc_bad[p.name] = fake.vlsit_tc_bad.get(p.name, 1) - 1
            p.write_text(_tc_text(p, bad or bool(fake.vlsit_tc_always_bad.get(p.name))))
        return StageResult("", TbUnitResult(summary="tcs"), 0.0, 1, "s")
    return None


MODULES = {
    "leaf_a": {"reqs": ["REQ-001"], "children": []},
    "leaf_b": {"reqs": ["REQ-002"], "children": []},
    "top": {"reqs": ["REQ-003"], "children": ["leaf_a", "leaf_b"]},
}
REQS = {"REQ-001": "a is registered", "REQ-002": "b is registered", "REQ-003": "top ties them"}

FLOW = """name: t
steps:
  - {id: rtl, kind: rtl_gen, options: {rules: [rtl_rule.md]}}
  - {id: testplan, kind: dv_testplan, gate: auto, deps: []}
  - {id: tb, kind: dv_tb, gate: auto, deps: [rtl, testplan]}
  - {id: simulate, kind: dv_simulate, gate: auto, deps: [tb]}
"""


def spec_json(with_modules=True, reqs=None, hints=None):
    reqs = reqs or REQS
    d = {"metadata": {"ip_name": "demo", "spec_revision": "0.1", "phase": 1, "gate_status": "approved", "top_module": "top"},
         "requirements": [{"req_id": r, "text": t, "category": "functional", "ambiguity_score": 0.1,
                           **({"sva_hint": hints[r]} if hints and r in hints else {})} for r, t in reqs.items()],
         "parameters": []}
    if with_modules:
        d["modules"] = [{"name": k, "description": f"the {k}", "req_ids": v["reqs"], "instances": v["children"]} for k, v in MODULES.items()]
    return d


def config_json(**params):
    return {"metadata": {"ip_name": "demo", "spec_revision": "0.1", "phase": 2, "gate_status": "approved", "gate_2_approved": True},
            "parameters": {k: {"value": str(v), "default": str(v), "type": "int", "changed_from_default": False, "confirmed_by_user": True}
                           for k, v in (params or {"PR_W": 8}).items()}}


@pytest.fixture
def llm(monkeypatch):
    fake = FakeLLM()
    fake.vlsit_modules = {k: dict(v) for k, v in MODULES.items()}
    fake.vlsit_bad = {}
    fake.vlsit_always_bad = {}
    fake.vlsit_questions = {}
    fake.vlsit_plan_calls = 0
    fake.vlsit_tb_plan_calls = 0
    fake.vlsit_triage_calls = 0
    fake.vlsit_triage_verdicts = None
    fake.vlsit_tb_plans = [[{"tc_id": "TC-001", "name": "a_reg", "description": "a", "req_ids": ["REQ-001", "REQ-003"]},
                            {"tc_id": "TC-002", "name": "b_reg", "description": "b", "req_ids": ["REQ-002"]}]]
    fake.vlsit_top_bad = 0
    fake.vlsit_tc_bad = {}
    fake.vlsit_tc_always_bad = {}
    monkeypatch.setattr(runtime, "run_stage", fake)
    return fake


def make_engine(tmp_path, tools_dir, extra_cfg="", synth_role=True, lint_role=True, spec=None, config=None, run_mode="pass") -> Engine:
    (tmp_path / "q3tui.yaml").write_text("pipeline: {flow: t}\n" + tools_yaml(tools_dir, synth_role, lint_role, run_mode) + extra_cfg)
    (tmp_path / "flows").mkdir(exist_ok=True)
    (tmp_path / "flows" / "t.yaml").write_text(FLOW)
    (tmp_path / "spec").mkdir(exist_ok=True)
    (tmp_path / "spec" / "spec.md").write_text("# demo spec\n")
    project = Project.open(tmp_path)
    artifacts.write(project, "structured_spec.json", spec or spec_json(), check=False)
    artifacts.write(project, "final_config.json", config or config_json(), check=False)
    return Engine(project)


def run_step(engine, name, **kw):
    return anyio.run(lambda: engine.run_step(engine.by_name[name], **kw))


def run_tb(engine, **kw):
    """The testbench of the plain_sv backend: its test plan (step testplan), then the testbench."""
    run_step(engine, "testplan", **kw)
    return run_step(engine, "tb", **kw)


def calls(llm, prefix):
    return [c for c in llm.calls if c.name.startswith(prefix)]


# -- rules ----------------------------------------------------------------------------------------------------

# -- rtl step ----------------------------------------------------------------------------------------------------

# -- tb step ------------------------------------------------------------------------------------------------------------

def with_rtl(tmp_path, eng, llm):
    run_step(eng, "rtl")


def test_tb_happy_path(tmp_path, tools_dir, llm):
    eng = make_engine(tmp_path, tools_dir)
    with_rtl(tmp_path, eng, llm)
    run_tb(eng)
    tb = tmp_path / "src" / "tb"
    assert sorted(p.name for p in (tb / "tests").glob("*.sv")) == ["tc_001_a_reg.sv", "tc_002_b_reg.sv"]
    assert "`define TC_CHECK" in (tb / "tb_common.svh").read_text()
    assert (tb / "tb_tests.svh").read_text().count("`include") == 2
    run = (tb / "tb_run.svh").read_text()
    assert 'tc_id_s = "TC-001"; tc_fail_f = 0;\ntc_001_a_reg();\n`TC_REPORT\nend' in run and "$value$plusargs" in run
    fl = (tb / "filelist.f").read_text().split("\n")
    assert not [x for x in fl if "tests/tc_" in x] and "src/tb/tb_top.sv" in fl  # the test cases are included by tb_top, never sources
    assert fl.index("src/rtl/leaf_a.sv") < fl.index("+incdir+src/tb") < fl.index("src/tb/tb_top.sv")
    assert (tb / "run.sh").is_file()
    plan = json.loads((tmp_path / "schemas" / "selected_testplan.json").read_text())
    assert artifacts.validate("selected_testplan.json", plan) == []
    assert plan["coverage"]["coverage_pct"] == 100.0 and plan["compile_check"]["status"] == "pass"
    assert plan["metadata"]["gate_4_approved"] is False and plan["result_format"] == "TC_RESULT <tc_id> PASS|FAIL"
    assert [t["compile_status"] for t in plan["test_cases"]] == ["pass", "pass"]
    # independence: spec + requirements only
    c = calls(llm, "tb_plan")[0]
    rtl, sva = tmp_path / "src" / "rtl", tmp_path / "src" / "sva"
    assert rtl in c.deny_dirs and sva in c.deny_dirs and c.builtin_tools == ["Read", "Grep", "Glob"]
    g = calls(llm, "tb_g1")[0]
    assert rtl in g.deny_dirs and g.write_files == [tb / "tests" / "tc_001_a_reg.sv", tb / "tests" / "tc_002_b_reg.sv"]
    assert "REQ-001: a is registered" in g.prompt
    # Gate 4 (auto): approving records it in the artifact
    eng.approve("tb")
    plan = json.loads((tmp_path / "schemas" / "selected_testplan.json").read_text())
    assert plan["metadata"]["gate_4_approved"] is True and plan["metadata"]["gate_4_at"]
    assert eng.evaluate(eng.by_name["tb"]).edited == []


def test_tb_conditional_test_cases_follow_the_configuration(tmp_path, tools_dir, llm):
    llm.vlsit_tb_plans = [[{"tc_id": "TC-001", "name": "a_reg", "req_ids": ["REQ-001", "REQ-003"]},
                           {"tc_id": "TC-002", "name": "m_ext", "req_ids": ["REQ-002"], "conditional_param": "PR_M_EXT_EN"}]]
    eng = make_engine(tmp_path, tools_dir, config=config_json(PR_W=8, PR_M_EXT_EN=0))
    run_tb(eng)
    plan = json.loads((tmp_path / "schemas" / "selected_testplan.json").read_text())
    assert [(t["tc_id"], t["selected"]) for t in plan["test_cases"]] == [("TC-001", True), ("TC-002", False)]
    assert not (tmp_path / "src/tb/tests/tc_002_m_ext.sv").exists()
    assert plan["coverage"]["uncovered_req_ids"] == ["REQ-002"]      # never silent
    assert plan["compile_check"]["status"] == "skipped"              # (no RTL was written in this test)


def test_tb_uncovered_requirement_asks_again_then_blocks(tmp_path, tools_dir, llm):
    llm.vlsit_tb_plans = [[{"tc_id": "TC-001", "name": "a_reg", "req_ids": ["REQ-001"]}]]
    eng = make_engine(tmp_path, tools_dir)
    run_tb(eng)
    assert llm.vlsit_tb_plan_calls == 2 and "Chưa có TC cover" in calls(llm, "tb_plan")[1].prompt
    qs = eng.by_name["testplan"].question_list(eng)
    assert len(qs) == 1 and qs[0]["blocking"] and "REQ-002, REQ-003" in qs[0]["question"] and eng.by_name["tb"].question_list(eng) == []
    # once answered, it is not asked again for the same requirements
    eng.answer(qs[0]["id"], "accept: SVA only")
    run_tb(eng)
    assert [q["status"] for q in eng.questions() if q["step"] == "testplan"] == ["answered"]
    # static / assumption requirements are not simulation coverage
    (tmp_path / "x").mkdir()
    eng2 = make_engine(tmp_path / "x", tools_dir, spec=spec_json(hints={"REQ-002": "static", "REQ-003": "assume"}))
    run_tb(eng2)
    assert [q for q in eng2.by_name["testplan"].question_list(eng2)] == []


def test_tb_compile_error_in_a_test_case_is_fixed(tmp_path, tools_dir, llm):
    llm.vlsit_tc_bad = {"tc_002_b_reg.sv": 1}
    eng = make_engine(tmp_path, tools_dir)
    with_rtl(tmp_path, eng, llm)
    run_tb(eng)
    fix = calls(llm, "tb_fix_tc_002_b_reg")
    assert len(fix) == 1 and "syntax error" in fix[0].prompt and "SYNTAX_ERR" in fix[0].prompt
    assert fix[0].write_files == [tmp_path / "src/tb/tests/tc_002_b_reg.sv"]
    plan = json.loads((tmp_path / "schemas" / "selected_testplan.json").read_text())
    assert plan["compile_check"]["status"] == "pass" and eng.by_name["tb"].question_list(eng) == []


def test_tb_persistent_compile_error_blocks_with_a_question(tmp_path, tools_dir, llm):
    llm.vlsit_tc_always_bad = {"tc_001_a_reg.sv": 1}
    eng = make_engine(tmp_path, tools_dir, extra_cfg="tb: {max_fix_attempts: 1}\n")
    with_rtl(tmp_path, eng, llm)
    run_tb(eng)
    plan = json.loads((tmp_path / "schemas" / "selected_testplan.json").read_text())
    assert plan["compile_check"]["status"] == "fail" and plan["test_cases"][0]["compile_status"] == "fail"
    qs = eng.by_name["tb"].question_list(eng)
    assert len(qs) == 1 and qs[0]["blocking"] and "does not compile" in qs[0]["question"]
    assert eng.state.gates["tb"].for_questions or eng.state.gates["tb"].status == "open"   # the auto gate waits for the answer


def test_tb_error_inside_the_rtl_is_not_fixed_by_the_testbench(tmp_path, tools_dir, llm):
    eng = make_engine(tmp_path, tools_dir)
    with_rtl(tmp_path, eng, llm)
    f = tmp_path / "src/rtl/leaf_a.sv"
    f.write_text(f.read_text() + "// RTL_ERR\n")
    n = len(llm.calls)
    run_tb(eng)
    assert not [c for c in llm.calls[n:] if c.name.startswith("tb_fix")]
    qs = eng.by_name["tb"].question_list(eng)
    assert qs and "leaf_a.sv" in qs[0]["question"]


def test_tb_top_contract_is_enforced(tmp_path, tools_dir, llm):
    llm.vlsit_top_bad = 1
    eng = make_engine(tmp_path, tools_dir)
    run_tb(eng)
    assert len(calls(llm, "tb_top_fix")) == 1 and "tb_tests.svh" in calls(llm, "tb_top_fix")[0].prompt
    assert tb_gen.top_problems((tmp_path / "src/tb/tb_top.sv").read_text()) == []


def test_tb_updates_only_what_changed(tmp_path, tools_dir, llm):
    eng = make_engine(tmp_path, tools_dir)
    run_tb(eng)
    n = len(llm.calls)
    run_tb(eng)
    assert len(llm.calls) == n                                      # nothing changed
    eng.request_change("tb", "[tb:TC-002] also check the reset value")
    run_tb(eng)
    new = llm.calls[n:]
    assert [c.name for c in new] == ["tb_g1"] and new[0].write_files == [tmp_path / "src/tb/tests/tc_002_b_reg.sv"]
    assert "also check the reset value" in new[0].prompt
    # a removed test case: its file goes (backed up)
    llm.vlsit_tb_plans = [[{"tc_id": "TC-001", "name": "a_reg", "req_ids": ["REQ-001", "REQ-002", "REQ-003"]}]]
    eng.request_change("testplan", "merge the tests")  # (the plan is the testplan step's: tb then rewrites what changed)
    run_tb(eng)
    assert not (tmp_path / "src/tb/tests/tc_002_b_reg.sv").exists() and (tmp_path / "src/tb/tests/tc_001_a_reg.sv").exists()
    assert json.loads((tmp_path / "schemas/selected_testplan.json").read_text())["coverage"]["coverage_pct"] == 100.0


# -- a tool that fails by itself is not the design's fault (real run: `vcs -lint=all` made every lint exit 2 with no
#    diagnostic; the step spent ~$2.7 of fix rounds rewriting correct RTL) ----------------------------------------------

def test_a_compile_that_fails_without_an_error_line_is_the_tools_problem(tmp_path, tools_dir, llm):
    from q3tui.pipeline.base import StepFailed

    eng = make_engine(tmp_path, tools_dir)
    run_step(eng, "rtl")
    (tools_dir / "sim.py").write_text("import sys\nprint('Make exited with status 2')\nsys.exit(2)\n")
    with pytest.raises(StepFailed) as exc:
        with_rtl(tmp_path, eng, llm)
        run_tb(eng)
    assert "`sim` tool exited with rc=2" in str(exc.value)
    assert not [c for c in llm.calls if c.name.startswith("tb_fix")]



# -- the test plan is a step of its own ---------------------------------------------------------------------------------------

def test_the_dv_flow_has_the_testplan_step_with_a_human_gate():
    from q3tui import flows

    flow = flows.load_flow("dv")
    assert [s.id for s in flow.steps] == ["spec", "parse", "config", "testplan", "tb", "simulate"] and flows.validate(flow) == []
    assert flow.spec("testplan").kind == "dv_testplan" and flow.spec("testplan").deps == ["config"] and flow.spec("testplan").gate == "human"
    assert flow.spec("tb").deps == ["testplan"]


def test_tb_waits_for_the_test_plan_and_rewrites_only_what_the_plan_changed(tmp_path, tools_dir, llm):
    eng = make_engine(tmp_path, tools_dir)
    assert "test plan is needed first" in eng.by_name["tb"].missing_input(eng)
    run_tb(eng)
    assert eng.by_name["tb"].missing_input(eng) is None
    n = len(llm.calls)
    # a plan that changes one test case: tb sees the changed input and rewrites that test case only
    llm.vlsit_tb_plans = [[{"tc_id": "TC-001", "name": "a_reg", "req_ids": ["REQ-001"]},
                           {"tc_id": "TC-002", "name": "b_reg", "req_ids": ["REQ-002", "REQ-003"], "description": "b and top, together"}]]
    eng.request_change("testplan", "check REQ-003 in the b test")
    run_tb(eng)
    new = [c.name for c in llm.calls[n:]]
    assert "tb_plan" in new and [c for c in new if c.startswith("tb_g")] == ["tb_g1"]
    assert (tmp_path / "src/tb/tests/tc_001_a_reg.sv").is_file() and (tmp_path / "src/tb/tests/tc_002_b_reg.sv").is_file()


# -- the kind of testbench ---------------------------------------------------------------------------------------------------------------------

def test_the_testbench_kind_follows_the_option_and_the_vip():
    from q3tui.flows.dv import uvm

    assert uvm.testbench_kind({}) == "plain" and uvm.testbench_kind(None) == "plain"  # (auto: no VIP, any IP)
    assert uvm.testbench_kind({"vip_path": "vip/axi"}) == "uvm" and uvm.testbench_kind({"uvm_vip": {"vip": {"path": "v"}}}) == "uvm"
    assert uvm.testbench_kind({"testbench": "plain", "vip_path": "vip/axi"}) == "plain" and uvm.testbench_kind({"testbench": "uvm"}) == "uvm"
    with pytest.raises(uvm.OptionsError, match="testbench"):
        uvm.testbench_kind({"testbench": "vmm"})


# -- the simulate step: every test case runs, the status comes from its log ----------------------------------------------------------------------------------

def run_all(eng):
    with_rtl_and = eng
    run_step(eng, "rtl")
    run_tb(eng)


def test_simulate_runs_every_test_case_and_reads_the_status_from_the_log(tmp_path, tools_dir, llm):
    eng = make_engine(tmp_path, tools_dir)
    sim = eng.by_name["simulate"]
    assert "testbench is not built yet" in sim.missing_input(eng)
    run_all(eng)
    assert sim.missing_input(eng) is None
    run_step(eng, "simulate")
    ev = json.loads((tmp_path / "schemas" / "dv_evidence.json").read_text())["entries"]
    assert [(e["name"], e["kind"], e["status"]) for e in ev] == [("TC-001", "run", "PASS"), ("TC-002", "run", "PASS")]
    assert all(e["log"].endswith(f"run_{e['name']}.log") for e in ev)
    md = (tmp_path / "reports" / "dv_test_results.md").read_text()
    assert "| TC-001 | `a_reg` | REQ-001, REQ-003 | PASS |" in md and "| TC-002 | `b_reg` | REQ-002 | PASS |" in md
    assert eng.evaluate(sim).status == "done"


@pytest.mark.parametrize("mode,why", [("failtc2", "the checked value was wrong"), ("nores", "no TC_RESULT line for TC-001")])
def test_a_test_case_that_fails_or_prints_no_result_is_a_fail_never_a_pass(tmp_path, tools_dir, llm, mode, why):
    eng = make_engine(tmp_path, tools_dir, run_mode=mode)
    run_all(eng)
    with pytest.raises(StepFailed, match="did not pass") as exc:
        run_step(eng, "simulate")
    assert why in str(exc.value) and "never a reason to change it" in str(exc.value)
    ev = json.loads((tmp_path / "schemas" / "dv_evidence.json").read_text())["entries"]
    assert "FAIL" in {e["status"] for e in ev} and any(e["status"] == "PASS" for e in ev) == (mode == "failtc2")


def test_simulate_waits_for_a_compiled_testbench_and_a_run_tool(tmp_path, tools_dir, llm):
    eng = make_engine(tmp_path, tools_dir)
    run_tb(eng)  # (no RTL yet: the compile check is skipped)
    assert "not compiled with the RTL" in eng.by_name["simulate"].missing_input(eng)
    run_step(eng, "rtl")
    run_tb(eng, regenerate=True) if False else run_step(eng, "tb")
    cfg = (tmp_path / "q3tui.yaml").read_text()
    (tmp_path / "q3tui.yaml").write_text("\n".join(l for l in cfg.splitlines() if not l.strip().startswith("run:")) + "\n")
    no_run = Engine(Project.open(tmp_path))
    assert "no tool for role 'run'" in no_run.by_name["simulate"].missing_input(no_run)


# -- a requirement no simulation can check is left out with its reason, not asked about -----------------------------------------------------------------

def test_a_requirement_the_planner_leaves_out_of_simulation_is_not_uncovered_and_not_a_question(tmp_path, tools_dir, llm):
    llm.vlsit_tb_plans = [[{"tc_id": "TC-001", "name": "a_reg", "req_ids": ["REQ-001"]}, {"tc_id": "TC-002", "name": "b_reg", "req_ids": ["REQ-002"]}]]
    llm.vlsit_tb_excluded = [{"req_id": "REQ-003", "reason": "a lint rule: closed by the lint step, not by simulating the DUT"}, {"req_id": "REQ-999", "reason": "unknown"}]
    eng = make_engine(tmp_path, tools_dir)
    run_step(eng, "testplan")
    assert llm.vlsit_tb_plan_calls == 1  # nothing uncovered: the planner is not asked again
    assert eng.by_name["testplan"].question_list(eng) == []  # no blocking question for REQ-003
    plan = json.loads((tmp_path / "schemas" / "test_plan.json").read_text())
    assert plan["excluded"] == [{"req_id": "REQ-003", "reason": "a lint rule: closed by the lint step, not by simulating the DUT"}]  # (the unknown id is dropped)
    assert plan["coverage"]["uncovered_req_ids"] == [] and plan["coverage"]["excluded_req_ids"] == ["REQ-003"] and plan["coverage"]["coverage_pct"] == 100.0
    run_step(eng, "tb")  # the testbench takes the same view of coverage
    assert json.loads((tmp_path / "schemas" / "selected_testplan.json").read_text())["coverage"]["uncovered_req_ids"] == []


def test_a_requirement_a_test_case_covers_is_not_left_out_and_a_plain_uncovered_one_still_asks(tmp_path, tools_dir, llm):
    llm.vlsit_tb_excluded = [{"req_id": "REQ-001", "reason": "wrongly left out"}]  # (TC-001 checks REQ-001)
    eng = make_engine(tmp_path, tools_dir)
    run_step(eng, "testplan")
    plan = json.loads((tmp_path / "schemas" / "test_plan.json").read_text())
    assert plan["excluded"] == [] and plan["coverage"]["coverage_pct"] == 100.0
    # a requirement that is neither covered nor left out: the planner is asked again, then the code's blocking question
    llm.vlsit_tb_plans = [[{"tc_id": "TC-001", "name": "a_reg", "req_ids": ["REQ-001"]}]]
    llm.vlsit_tb_excluded = []
    eng2 = make_engine(tmp_path / "x", tools_dir) if (tmp_path / "x").mkdir() is None else None
    run_step(eng2, "testplan")
    qs = eng2.by_name["testplan"].question_list(eng2)
    assert len(qs) == 1 and qs[0]["blocking"] and "REQ-002, REQ-003" in qs[0]["question"]


def test_the_plan_prompt_tells_a_testbench_choice_from_a_spec_gap():
    from q3tui.flows.dv.plain import task

    s = task.PLAN_SYSTEM
    assert "`design_choice`" in s and "spec_gap" in s and "không đổi spec và không đổi RTL" in s  # (how the testbench checks something stays in the plan)


def test_no_stage_is_denied_a_file_it_must_write(tmp_path, tools_dir, llm):
    """The fake LLM does not enforce the file guard, a real run does: a stage whose write_files / write_dirs sit inside its deny_dirs is blocked
    ("is not accessible to this stage"): the testbench folder was denied to the stages that write it."""
    eng = make_engine(tmp_path, tools_dir)
    llm.vlsit_top_bad = 1  # (the fix stages too)
    llm.vlsit_tc_bad = {"tc_002_b_reg.sv": 1}
    run_all(eng)
    writers = [c for c in llm.calls if c.write_files or c.write_dirs]
    assert {c.name for c in writers} >= {"tb_top", "tb_top_fix", "tb_g1"}
    for c in writers:
        targets = [*(c.write_files or []), *(c.write_dirs or [])]
        assert targets and not [(f, d) for f in targets for d in c.deny_dirs if Path(f).resolve().is_relative_to(Path(d).resolve())], c.name


# -- a failing test is judged: the testbench goes back to tb, the RTL to the rtl flow, the spec to you ---------------------------------------------------------

def failing(tmp_path, tools_dir, llm, verdicts, run_mode="failtc2"):
    llm.vlsit_triage_verdicts = verdicts
    eng = make_engine(tmp_path, tools_dir, run_mode=run_mode)
    run_all(eng)
    return eng


def test_a_testbench_bug_goes_back_to_tb_scoped_to_the_test_and_the_chain_runs_again(tmp_path, tools_dir, llm):
    eng = failing(tmp_path, tools_dir, llm, [{"id": "TC-002", "verdict": "tb_bug", "summary": "the check compares the wrong value", "request": "compare with the value the spec gives"}])
    run_step(eng, "simulate")  # (no failure: the fix is dispatched and the engine's loop runs it)
    assert llm.vlsit_triage_calls == 1
    fb = eng.state.feedback["tb"]
    assert len(fb) == 1 and fb[0].startswith("[tb:TC-002] From the simulation triage (TC-002): the check compares the wrong value compare with the value the spec gives")
    meta = eng.state.step_meta["simulate"]
    assert meta["loop"] is True and meta["rounds"] == 1
    report = json.loads((tmp_path / "schemas" / "dv_triage.json").read_text())
    assert report["round"] == 1 and report["verdicts"][0]["verdict"] == "tb_bug" and report["failures"][0]["id"] == "TC-002"
    # the loop: tb rewrites that test case only, the tests pass, the rounds start afresh
    (tmp_path / "q3tui.yaml").write_text((tmp_path / "q3tui.yaml").read_text().replace("failtc2", "pass"))
    eng2 = Engine(Project.open(tmp_path))
    n = len(llm.calls)
    run_step(eng2, "tb")
    new = [c for c in llm.calls[n:] if c.write_files]
    assert [c.name for c in new] == ["tb_g1"] and new[0].write_files == [tmp_path / "src/tb/tests/tc_002_b_reg.sv"] and "compare with the value the spec gives" in new[0].prompt
    run_step(eng2, "simulate")
    assert eng2.state.step_meta["simulate"]["rounds"] == 0 and not eng2.state.step_meta["simulate"]["loop"] and "history" not in eng2.state.step_meta["simulate"]
    assert {e["name"]: e["status"] for e in json.loads((tmp_path / "schemas" / "dv_evidence.json").read_text())["entries"]} == {"TC-001": "PASS", "TC-002": "PASS"}


def test_an_rtl_bug_is_handed_to_the_rtl_flow_and_the_step_waits(tmp_path, tools_dir, llm):
    from q3tui import flows

    eng = failing(tmp_path, tools_dir, llm, [{"id": "TC-002", "verdict": "rtl_bug", "module": "leaf_b", "summary": "b is not registered", "request": "register b on the clock edge"}])
    with pytest.raises(StepFailed, match="handed to the rtl flow") as exc:
        run_step(eng, "simulate")
    assert "leaf_b" in str(exc.value) and "tb and simulate run on the new RTL" in str(exc.value)
    rtl_flow = Engine(Project.open(tmp_path), flow=flows.load_flow("rtl"))  # the other flow's engine reads the same state
    assert [f for f in rtl_flow.state.feedback.get("rtl", [])] == ["[rtl:leaf_b] From the simulation of the testbench (TC-002): b is not registered register b on the clock edge"]
    assert not eng.state.feedback.get("tb") and not eng.state.step_meta["simulate"].get("loop")  # (the testbench is not touched)


@pytest.mark.parametrize("verdict,kind", [("spec_issue", "spec_gap"), ("unsure", "design_choice")])
def test_a_spec_issue_or_an_unsure_verdict_is_a_blocking_question_and_nothing_changes(tmp_path, tools_dir, llm, verdict, kind):
    eng = failing(tmp_path, tools_dir, llm, [{"id": "TC-002", "verdict": verdict, "summary": "the spec does not say when b is visible", "request": "assume one cycle"}])
    with pytest.raises(StepFailed, match="need your decision"):
        run_step(eng, "simulate")
    qs = eng.by_name["simulate"].question_list(eng)
    assert len(qs) == 1 and qs[0]["blocking"] and qs[0]["kind"] == kind and "TC-002" in qs[0]["question"] and qs[0]["default_assumption"] == "assume one cycle"
    assert not eng.state.feedback.get("tb") and not eng.state.feedback.get("rtl")


def test_the_same_testbench_fix_coming_back_is_a_question_not_another_round(tmp_path, tools_dir, llm):
    v = [{"id": "TC-002", "verdict": "tb_bug", "summary": "still the wrong value", "request": "use the spec's value"}]
    eng = failing(tmp_path, tools_dir, llm, v)
    run_step(eng, "simulate")
    run_step(eng, "simulate")  # (the same verdict again: handed over twice)
    with pytest.raises(StepFailed, match="need your decision"):  # the third time: it is not a thing another round settles
        run_step(eng, "simulate")
    qs = eng.by_name["simulate"].question_list(eng)
    assert len(qs) == 1 and "keeps failing" in qs[0]["question"] and qs[0]["kind"] == "design_choice"
    assert len(eng.state.feedback["tb"]) == 2


def test_the_fix_rounds_are_bounded(tmp_path, tools_dir, llm):
    eng = failing(tmp_path, tools_dir, llm, [{"id": "TC-002", "verdict": "tb_bug", "summary": "x", "request": "y"}])
    eng.project.cfg.pipeline.max_fix_iterations = 1
    run_step(eng, "simulate")
    with pytest.raises(StepFailed, match="still failing after 1 fix round"):
        run_step(eng, "simulate")


def test_without_a_triage_the_failure_is_reported_as_it_was(tmp_path, tools_dir, llm):
    eng = failing(tmp_path, tools_dir, llm, None)  # (no credentials)
    with pytest.raises(StepFailed, match="did not pass") as exc:
        run_step(eng, "simulate")
    assert "TC-002" in str(exc.value) and "the triage could not run" in str(exc.value) and llm.vlsit_triage_calls == 1


def test_the_triage_method_is_the_sim_triage_skill_and_yours_wins(tmp_path, tools_dir, llm):
    eng = failing(tmp_path, tools_dir, llm, [{"id": "TC-002", "verdict": "tb_bug", "summary": "x", "request": "y"}])
    run_step(eng, "simulate")
    (stage,) = [c for c in llm.calls if c.name == "dv_triage"]
    assert "Read the skill `sim-triage`" in stage.system_prompt and "sim-triage" in stage.prompt and "flows/dv/skills/sim-triage/SKILL.md" in stage.prompt.replace("\\", "/")
    assert "demo spec" in stage.prompt and "TC-002" in stage.prompt and "log " in stage.prompt  # (the spec, the failing test and the end of its log)
    assert stage.builtin_tools == ["Read", "Grep", "Glob"] and tmp_path / "src" / "rtl" not in stage.deny_dirs and tmp_path / "src" / "tb" not in stage.deny_dirs  # (it may read the RTL and the testbench)
    mine = tmp_path / "config" / "skills" / "sim-triage"
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("---\nname: sim-triage\ndescription: my own triage rules\n---\n# mine\n")
    llm.calls.clear()
    run_step(Engine(Project.open(tmp_path)), "simulate")
    (stage,) = [c for c in llm.calls if c.name == "dv_triage"]
    assert "my own triage rules" in stage.prompt and "config/skills/sim-triage/SKILL.md" in stage.prompt.replace("\\", "/")
