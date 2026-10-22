"""The dv flow's `tb` step, plain_sv backend (flows/dv/tb/): fake LLM stages, fake tools (scripts standing in
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
from q3tui.flows.dv.tb import gen as tb_gen
from q3tui.flows.dv.tb import plan as tb_plan
from q3tui.flows.rtl.rtl.plan import RtlPlan, PlanModule, levels, plan_from_spec
from q3tui.flows.rtl.rtl.gen import VModuleResult
from q3tui.flows.dv.tb.plan import TbCase, TbPlan, TbUnitResult
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
    return d


def tools_yaml(d: Path, synth_role=True, lint_role=True) -> str:
    py = sys.executable
    roles = []
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
    if n == "tb_plan":
        fake.vlsit_tb_plan_calls += 1
        cases = fake.vlsit_tb_plans.pop(0) if len(fake.vlsit_tb_plans) > 1 else fake.vlsit_tb_plans[0]
        return StageResult("", TbPlan(test_cases=[TbCase(**c) for c in cases]), 0.0, 1, "s")
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
  - {id: tb, kind: dv_tb, gate: auto, deps: [rtl]}
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
    fake.vlsit_tb_plans = [[{"tc_id": "TC-001", "name": "a_reg", "description": "a", "req_ids": ["REQ-001", "REQ-003"]},
                            {"tc_id": "TC-002", "name": "b_reg", "description": "b", "req_ids": ["REQ-002"]}]]
    fake.vlsit_top_bad = 0
    fake.vlsit_tc_bad = {}
    fake.vlsit_tc_always_bad = {}
    monkeypatch.setattr(runtime, "run_stage", fake)
    return fake


def make_engine(tmp_path, tools_dir, extra_cfg="", synth_role=True, lint_role=True, spec=None, config=None) -> Engine:
    (tmp_path / "q3tui.yaml").write_text("pipeline: {flow: t}\n" + tools_yaml(tools_dir, synth_role, lint_role) + extra_cfg)
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
    run_step(eng, "tb")
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
    run_step(eng, "tb")
    plan = json.loads((tmp_path / "schemas" / "selected_testplan.json").read_text())
    assert [(t["tc_id"], t["selected"]) for t in plan["test_cases"]] == [("TC-001", True), ("TC-002", False)]
    assert not (tmp_path / "src/tb/tests/tc_002_m_ext.sv").exists()
    assert plan["coverage"]["uncovered_req_ids"] == ["REQ-002"]      # never silent
    assert plan["compile_check"]["status"] == "skipped"              # (no RTL was written in this test)


def test_tb_uncovered_requirement_asks_again_then_blocks(tmp_path, tools_dir, llm):
    llm.vlsit_tb_plans = [[{"tc_id": "TC-001", "name": "a_reg", "req_ids": ["REQ-001"]}]]
    eng = make_engine(tmp_path, tools_dir)
    run_step(eng, "tb")
    assert llm.vlsit_tb_plan_calls == 2 and "Chưa có TC cover" in calls(llm, "tb_plan")[1].prompt
    qs = eng.by_name["tb"].question_list(eng)
    assert len(qs) == 1 and qs[0]["blocking"] and "REQ-002, REQ-003" in qs[0]["question"]
    # once answered, it is not asked again for the same requirements
    eng.answer(qs[0]["id"], "accept: SVA only")
    run_step(eng, "tb")
    assert [q["status"] for q in eng.questions() if q["step"] == "tb"] == ["answered"]
    # static / assumption requirements are not simulation coverage
    (tmp_path / "x").mkdir()
    eng2 = make_engine(tmp_path / "x", tools_dir, spec=spec_json(hints={"REQ-002": "static", "REQ-003": "assume"}))
    run_step(eng2, "tb")
    assert [q for q in eng2.by_name["tb"].question_list(eng2)] == []


def test_tb_compile_error_in_a_test_case_is_fixed(tmp_path, tools_dir, llm):
    llm.vlsit_tc_bad = {"tc_002_b_reg.sv": 1}
    eng = make_engine(tmp_path, tools_dir)
    with_rtl(tmp_path, eng, llm)
    run_step(eng, "tb")
    fix = calls(llm, "tb_fix_tc_002_b_reg")
    assert len(fix) == 1 and "syntax error" in fix[0].prompt and "SYNTAX_ERR" in fix[0].prompt
    assert fix[0].write_files == [tmp_path / "src/tb/tests/tc_002_b_reg.sv"]
    plan = json.loads((tmp_path / "schemas" / "selected_testplan.json").read_text())
    assert plan["compile_check"]["status"] == "pass" and eng.by_name["tb"].question_list(eng) == []


def test_tb_persistent_compile_error_blocks_with_a_question(tmp_path, tools_dir, llm):
    llm.vlsit_tc_always_bad = {"tc_001_a_reg.sv": 1}
    eng = make_engine(tmp_path, tools_dir, extra_cfg="tb: {max_fix_attempts: 1}\n")
    with_rtl(tmp_path, eng, llm)
    run_step(eng, "tb")
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
    run_step(eng, "tb")
    assert not [c for c in llm.calls[n:] if c.name.startswith("tb_fix")]
    qs = eng.by_name["tb"].question_list(eng)
    assert qs and "leaf_a.sv" in qs[0]["question"]


def test_tb_top_contract_is_enforced(tmp_path, tools_dir, llm):
    llm.vlsit_top_bad = 1
    eng = make_engine(tmp_path, tools_dir)
    run_step(eng, "tb")
    assert len(calls(llm, "tb_top_fix")) == 1 and "tb_tests.svh" in calls(llm, "tb_top_fix")[0].prompt
    assert tb_gen.top_problems((tmp_path / "src/tb/tb_top.sv").read_text()) == []


def test_tb_updates_only_what_changed(tmp_path, tools_dir, llm):
    eng = make_engine(tmp_path, tools_dir)
    run_step(eng, "tb")
    n = len(llm.calls)
    run_step(eng, "tb")
    assert len(llm.calls) == n                                      # nothing changed
    eng.request_change("tb", "[tb:TC-002] also check the reset value")
    run_step(eng, "tb")
    new = llm.calls[n:]
    assert [c.name for c in new] == ["tb_g1"] and new[0].write_files == [tmp_path / "src/tb/tests/tc_002_b_reg.sv"]
    assert "also check the reset value" in new[0].prompt
    # a removed test case: its file goes (backed up)
    llm.vlsit_tb_plans = [[{"tc_id": "TC-001", "name": "a_reg", "req_ids": ["REQ-001", "REQ-002", "REQ-003"]}]]
    eng.request_change("tb", "[tb:plan] merge the tests")
    run_step(eng, "tb")
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
        run_step(eng, "tb")
    assert "`sim` tool exited with rc=2" in str(exc.value)
    assert not [c for c in llm.calls if c.name.startswith("tb_fix")]

