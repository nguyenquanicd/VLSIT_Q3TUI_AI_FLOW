"""The dv flow with the plain SystemVerilog testbench (flows/dv/plain/, the steps testplan, tb, simulate): fake LLM stages, fake tools (scripts standing in
for Verilator / Icarus / Yosys), no live LLM and no EDA tool."""

import contextlib
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
import os
tc, mode = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "pass")
built = len(sys.argv) > 3 and os.path.isfile(os.path.join(sys.argv[3], "build/tb/programs/b.list"))
if mode == "nores":
    print("simulation ended")
elif mode == "noprog" and tc == "TC-002" and not built:   # (a VCS-style warning and the testbench's own message: the program was never built)
    print("Warning-[STASKW_CO] Cannot open file"); print("  The file 'build/tb/programs/b.list' could not be opened. No such file or directory")
    print(f"TC_DETAIL {tc} FAIL: build/tb/programs/b.list not found"); print(f"TC_RESULT {tc} FAIL")
elif mode == "stamp":   # (when each run started and ended: do they overlap)
    import time; t0 = time.time(); time.sleep(1.0)
    open(os.path.join(sys.argv[3], "stamps.txt"), "a").write(f"{tc} {t0} {time.time()}\\n"); print(f"TC_RESULT {tc} PASS")
elif mode == "slow" and tc == "TC-002":
    import time; time.sleep(30)
elif mode == "failslow":   # (TC-002 fails at once, TC-001 takes long)
    import time
    if tc == "TC-002":
        print(f"TC_DETAIL {tc} FAIL: still wrong"); print(f"TC_RESULT {tc} FAIL")
    else:
        time.sleep(30); print(f"TC_RESULT {tc} PASS")
elif mode == "fail" or (mode == "failtc2" and tc == "TC-002"):
    print(f"TC_DETAIL {tc} FAIL: the checked value was wrong"); print(f"TC_RESULT {tc} FAIL")
else:
    print(f"TC_RESULT {tc} PASS")
"""


def tools_yaml(d: Path, synth_role=True, lint_role=True, run_mode="pass") -> str:
    py = sys.executable
    roles = [f"    run: {{cmd: '{py} {d}/run.py {{tc}} {run_mode} {{project}}', parse: generic}}"]
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
    if n == "prepare" or n.startswith("prepare_fix"):
        from q3tui.flows.rtl.lib.models import PrepareResult, TestInputs

        fake.vlsit_prepare_calls = getattr(fake, "vlsit_prepare_calls", 0) + 1
        bad = getattr(fake, "vlsit_prepare_bad", 0) > 0
        fake.vlsit_prepare_bad = max(0, getattr(fake, "vlsit_prepare_bad", 0) - 1)
        make = ("all:\n\tmkdir -p $(PROG_DIR)\n" if bad else
                "all:\n\tmkdir -p $(PROG_DIR)\n\t$(RISCV_PREFIX)gcc -o $(PROG_DIR)/b.elf b.S\n\t$(SPIKE) $(PROG_DIR)/b.elf > $(PROG_DIR)/b.list\n")
        (stage.write_dirs[0] / "Makefile").write_text(make)
        (stage.write_dirs[0] / "b.S").write_text("_start: j _start\n")
        tests = getattr(fake, "vlsit_prepare_tests", [{"tc_id": "TC-002", "files": ["b.list"]}])
        return StageResult("", PrepareResult(prog_dir="build/tb/programs", tests=[TestInputs(**t) for t in tests]), 0.0, 1, "s")
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
    assert [s.id for s in flow.steps] == ["spec", "parse", "config", "testplan", "tb", "prepare", "simulate"] and flows.validate(flow) == []
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


def test_a_test_whose_input_files_are_not_in_the_project_is_blocked_not_triaged(tmp_path, tools_dir, llm):
    eng = make_engine(tmp_path, tools_dir, run_mode="noprog")
    run_all(eng)
    run_step(eng, "simulate")                                               # blocked alone: the step is done, with a warning
    ev = {e["name"]: e for e in json.loads((tmp_path / "schemas" / "dv_evidence.json").read_text())["entries"]}
    assert ev["TC-001"]["status"] == "PASS" and ev["TC-002"]["status"] == "BLOCKED" and "build/tb/programs/b.list" in ev["TC-002"]["note"]
    assert not [c for c in llm.calls if c.name == "dv_triage"] and not eng.state.feedback.get("tb")   # nothing handed back to the testbench
    assert "| TC-002 | `b_reg` | REQ-002 | BLOCKED |" in (tmp_path / "reports" / "dv_test_results.md").read_text()


def test_missing_inputs_are_files_that_exist_nowhere_in_the_project(tmp_path):
    from q3tui.flows.dv.plain.simulate import missing_inputs

    (tmp_path / "src/tb").mkdir(parents=True)
    (tmp_path / "src/tb/filelist.f").write_text("")
    log = ("The file '../src/tb/filelist.f' could not be opened.\n"                 # probed at a wrong relative path, but the file is there
           "The file 'build/tb/programs/a.list' could not be opened.\nTC_DETAIL TC-1 FAIL: ../build/tb/programs/a.list not found\n"
           "TC_DETAIL TC-1 FAIL: missing golden export (per stem .image.hex/.act1.hex/.logits.hex)\nTC_DETAIL TC-1 FAIL: read a.list: value 3, expected 4\n")
    assert missing_inputs(log, tmp_path) == ["../build/tb/programs/a.list"]   # (a list of suffixes is not a file)
    probed = ("Warning-[STASKW_CO] Cannot open file\n  The file 'build/tb/programs/p.xskip' could not be opened. No such file or directory\n"
              "TC_DETAIL TC-1 FAIL: x5 = 3, expected 4\n")
    assert missing_inputs(probed, tmp_path) == []                            # an optional file the test went on without is not why it failed
    assert missing_inputs("The file 'build/tb/programs/p.imem.hex' could not be opened.\n", tmp_path) == ["build/tb/programs/p.imem.hex"]  # (no TC_DETAIL: any line)


@pytest.mark.parametrize("jobs", [1, 2])
def test_the_simulations_run_in_parallel_up_to_sim_parallel_and_the_results_keep_the_plan_order(tmp_path, tools_dir, llm, jobs):
    eng = make_engine(tmp_path, tools_dir, run_mode="stamp", extra_cfg=f"tb: {{sim_parallel: {jobs}}}\n")
    run_all(eng)
    run_step(eng, "simulate")
    (a, a0, a1), (b, b0, b1) = sorted((t, float(x), float(y)) for t, x, y in (l.split() for l in (tmp_path / "stamps.txt").read_text().splitlines()))
    assert (max(a0, b0) < min(a1, b1)) == (jobs == 2)                        # overlapping only when two may run at once
    ev = json.loads((tmp_path / "schemas" / "dv_evidence.json").read_text())["entries"]
    assert [e["name"] for e in ev if e["kind"] == "run"] == ["TC-001", "TC-002"] and all(e["status"] == "PASS" for e in ev if e["kind"] == "run")


def test_every_test_has_its_status_while_the_tests_run_and_the_last_evidence_stays_until_they_end(tmp_path, tools_dir, llm, monkeypatch):
    from q3tui.flows.dv.plain import simulate as sim

    eng = make_engine(tmp_path, tools_dir, extra_cfg="tb: {sim_parallel: 1}\n")
    run_all(eng)
    run_step(eng, "simulate")
    live = json.loads((tmp_path / "schemas" / "dv_test_results.json").read_text())
    assert [(t["tc_id"], t["status"], t["last"]) for t in live["tests"]] == [("TC-001", "PASS", None), ("TC-002", "PASS", None)] and live["ended"]
    assert live["tests"][0]["log"].endswith("run_TC-001.log") and live["tests"][0]["duration_s"] is not None
    seen, write = [], sim._Live.write

    def spy(self):
        write(self)
        seen.append((tuple(t["status"] for t in self.data["tests"]), (tmp_path / "schemas" / "dv_evidence.json").is_file()))

    monkeypatch.setattr(sim._Live, "write", spy)
    run_step(eng, "simulate")
    assert [s for s, _ in seen] == [("PENDING", "PENDING"), ("RUNNING", "PENDING"), ("PASS", "PENDING"), ("PASS", "RUNNING"), ("PASS", "PASS"), ("PASS", "PASS")]
    assert all(ev for _, ev in seen)                                         # the evidence of the run before is there until the tests are over
    first = [t["duration_s"] for t in live["tests"]]
    live = json.loads((tmp_path / "schemas" / "dv_test_results.json").read_text())
    assert [t["last"] for t in live["tests"]] == ["PASS", "PASS"]           # (the result of the run before)
    assert [t["est_s"] for t in live["tests"]] == first and live["jobs"] == 1 and live["order"] == ["TC-001", "TC-002"]   # (its time then: the estimate)


def test_without_a_run_before_the_estimate_is_the_evidence_s_time(tmp_path):
    from q3tui.flows.dv.plain import simulate as sim

    (tmp_path / "dv_evidence.json").write_text(json.dumps({"entries": [{"kind": "run", "name": "TC-001", "duration_s": 42.0},
                                                                       {"kind": "compile", "name": "TC-002", "duration_s": 9.0}]}))
    live = sim._Live(tmp_path / "dv_test_results.json", [{"tc_id": "TC-001"}, {"tc_id": "TC-002"}], jobs=4)
    assert [t["est_s"] for t in live.data["tests"]] == [42.0, None] and live.data["jobs"] == 4


def test_a_run_that_stops_leaves_its_unfinished_tests_not_run(tmp_path):
    from q3tui.flows.dv.plain import simulate as sim

    live = sim._Live(tmp_path / "dv_test_results.json", [{"tc_id": "TC-001"}, {"tc_id": "TC-002"}])
    live.set("TC-001", status="PASS")
    live.set("TC-002", status="RUNNING")
    live.close()
    data = json.loads((tmp_path / "dv_test_results.json").read_text())
    assert [t["status"] for t in data["tests"]] == ["PASS", "NOT_RUN"] and data["ended"]


def test_sim_parallel_defaults_to_80_percent_of_the_cores(monkeypatch):
    from q3tui.core.config import TBConfig

    monkeypatch.setattr("os.cpu_count", lambda: 24)
    assert TBConfig().sim_jobs() == 19 and TBConfig(sim_parallel=3).sim_jobs() == 3
    monkeypatch.setattr("os.cpu_count", lambda: 1)
    assert TBConfig().sim_jobs() == 1


def test_a_test_that_runs_past_the_time_limit_is_a_timeout_and_the_other_tests_still_run(tmp_path, tools_dir, llm):
    eng = make_engine(tmp_path, tools_dir, run_mode="slow")
    cfg = (tmp_path / "q3tui.yaml").read_text().replace("parse: generic}", "parse: generic, timeout_s: 2}", 1)
    (tmp_path / "q3tui.yaml").write_text(cfg)
    eng = Engine(Project.open(tmp_path))
    run_all(eng)
    run_step(eng, "simulate")                                               # not a broken tool: the step finishes
    ev = {e["name"]: e for e in json.loads((tmp_path / "schemas" / "dv_evidence.json").read_text())["entries"]}
    assert ev["TC-001"]["status"] == "PASS" and ev["TC-002"]["status"] == "TIMEOUT" and "timeout_s" in ev["TC-002"]["note"]
    assert not [c for c in llm.calls if c.name == "dv_triage"] and not eng.state.feedback.get("tb")   # not judged, not handed back
    assert "| TC-002 | `b_reg` | REQ-002 | TIMEOUT |" in (tmp_path / "reports" / "dv_test_results.md").read_text()


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


def test_the_failures_that_wait_for_one_decision_are_one_question_with_the_ways_to_settle_it_as_its_pick_list(tmp_path, tools_dir, llm):
    opts = [{"label": "Allow changing the generated block in place: a write commits at the PREADY = 1 edge", "description": "the spec stays", "impact": "no area change"},
            {"label": "Revise B16: a write commits at the edge ending the first ACCESS cycle", "description": "the spec follows the block", "impact": "hardware wins a same-cycle set"}]
    eng = failing(tmp_path, tools_dir, llm, [{"id": "TC-001", "verdict": "spec_issue", "summary": "the write commits one edge early", "decision": "DEC-012", "options": opts},
                                             {"id": "TC-002", "verdict": "spec_issue", "summary": "the same early commit", "decision": "dec-012", "options": opts}], run_mode="fail")
    with pytest.raises(StepFailed, match="need your decision on SIM1"):
        run_step(eng, "simulate")
    (q,) = eng.by_name["simulate"].question_list(eng)
    assert q["question"].startswith("Tests TC-001, TC-002 (DEC-012): the write commits one edge early") and q["kind"] == "spec_gap" and q["blocking"]
    assert [o["label"] for o in q["options"]] == [o["label"] for o in opts] and q["options"][1]["impact"] == "hardware wins a same-cycle set"
    assert q["default_assumption"] == opts[0]["label"]                                 # (the recommended one: marked in the picker)


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


def _with_run_mode(tmp_path, mode: str) -> Engine:
    cfg = (tmp_path / "q3tui.yaml").read_text()
    (tmp_path / "q3tui.yaml").write_text(re.sub(r"(run\.py \{tc\} )\w+", rf"\g<1>{mode}", cfg))
    return Engine(Project.open(tmp_path))


@pytest.mark.parametrize("jobs", [1, 2])
def test_after_a_fix_the_tests_that_failed_run_first_and_one_that_fails_again_stops_the_others(tmp_path, tools_dir, llm, jobs):
    import time

    llm.vlsit_triage_verdicts = [{"id": "TC-002", "verdict": "tb_bug", "summary": "x", "request": "y"}]
    eng = make_engine(tmp_path, tools_dir, run_mode="failtc2", extra_cfg=f"tb: {{sim_parallel: {jobs}}}\n")
    run_all(eng)
    run_step(eng, "simulate")
    from q3tui.flows.dv.plain import simulate as sim

    assert sim.bad_before(tmp_path / "schemas" / "dv_test_results.json", {"TC-001", "TC-002"}) == {"TC-002": "FAIL"}
    eng = _with_run_mode(tmp_path, "failslow")                                     # (the fix did not work; TC-001 would take 30 s)
    t0 = time.monotonic()
    run_step(eng, "simulate")
    assert time.monotonic() - t0 < 15                                               # TC-001 was not waited for
    live = {t["tc_id"]: t for t in json.loads((tmp_path / "schemas" / "dv_test_results.json").read_text())["tests"]}
    assert live["TC-002"]["status"] == "FAIL" and live["TC-001"]["status"] == "NOT_RUN"
    assert live["TC-001"]["note"].startswith("not run:" if jobs == 1 else "stopped after") and "TC-002 failed again" in live["TC-001"]["note"]
    ev = json.loads((tmp_path / "schemas" / "dv_evidence.json").read_text())["entries"]
    assert [e["name"] for e in ev] == ["TC-002"]                                    # (a stopped run is no evidence)
    assert "NOT_RUN: stopped, or not started, because TC-002 failed again" in (tmp_path / "reports" / "dv_test_results.md").read_text()
    assert llm.vlsit_triage_calls == 2 and eng.state.step_meta["simulate"]["loop"] is True       # the fix loop goes on with TC-002


def test_after_a_fix_that_worked_the_other_tests_run_as_usual(tmp_path, tools_dir, llm):
    llm.vlsit_triage_verdicts = [{"id": "TC-002", "verdict": "tb_bug", "summary": "x", "request": "y"}]
    eng = make_engine(tmp_path, tools_dir, run_mode="failtc2", extra_cfg="tb: {sim_parallel: 1}\n")
    run_all(eng)
    run_step(eng, "simulate")
    eng = _with_run_mode(tmp_path, "stamp")
    run_step(eng, "simulate")
    order = [ln.split()[0] for ln in (tmp_path / "stamps.txt").read_text().splitlines()]
    assert order == ["TC-002", "TC-001"]                                             # the one that failed before, first
    assert [t["status"] for t in json.loads((tmp_path / "schemas" / "dv_test_results.json").read_text())["tests"]] == ["PASS", "PASS"]


def test_what_failed_before_comes_from_the_status_file_also_after_a_run_that_was_stopped(tmp_path):
    from q3tui.flows.dv.plain import simulate as sim

    f = tmp_path / "dv_test_results.json"
    f.write_text(json.dumps({"ended": None, "tests": [
        {"tc_id": "TC-001", "status": "PASS", "last": "FAIL"},                      # fixed: not first any more
        {"tc_id": "TC-002", "status": "PENDING", "last": "FAIL"},                   # (the run was stopped before it: its result before)
        {"tc_id": "TC-003", "status": "FAIL", "last": "PASS"},
        {"tc_id": "TC-004", "status": "NOT_RUN", "last": "BLOCKED"},
        {"tc_id": "TC-005", "status": "RUNNING", "last": "PASS"},
        {"tc_id": "TC-009", "status": "FAIL", "last": None}]}))                      # (no longer in the plan)
    assert sim.bad_before(f, {"TC-001", "TC-002", "TC-003", "TC-004", "TC-005"}) == {"TC-002": "FAIL", "TC-003": "FAIL", "TC-004": "BLOCKED"}
    assert sim.bad_before(tmp_path / "none.json", {"TC-001"}) == {}


def test_a_stop_kills_a_running_tool(tmp_path):
    import threading
    import time

    from q3tui.eda.base import run_command

    stop = threading.Event()
    threading.Timer(0.5, stop.set).start()
    t0 = time.monotonic()
    rc, out, secs, timed_out = run_command([sys.executable, "-c", "import time; print('go', flush=True); time.sleep(30)"], tmp_path, tmp_path / "x.log", 60, stop=stop)
    assert time.monotonic() - t0 < 10 and rc is None and not timed_out and "stopped" in out


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


def test_the_triage_knows_which_modules_are_generated_and_whether_they_may_be_changed(tmp_path, tools_dir, llm):
    eng = failing(tmp_path, tools_dir, llm, [{"id": "TC-002", "verdict": "tb_bug", "summary": "x", "request": "y"}])
    run_step(eng, "simulate")
    (stage,) = [c for c in llm.calls if c.name == "dv_triage"]
    assert "## Generated RTL" not in stage.prompt                                    # (no generated module)
    (tmp_path / "src" / "rtl" / "csr").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "rtl" / "csr" / "provided.json").write_text(json.dumps({"m_blk_csr": {"kind": "csr", "parent": "top", "req_ids": [], "instances": []}}))
    for keep, said in ((False, "a bug in one is an rtl_bug for it"), (True, "they are never changed")):
        eng = Engine(Project.open(tmp_path))
        eng.project.cfg.pipeline.step_options["rtl"] = {"keep_generated": keep}
        llm.calls.clear()
        with contextlib.suppress(StepFailed):                                        # (the same verdict again: a question, after the triage)
            run_step(eng, "simulate")
        (stage,) = [c for c in llm.calls if c.name == "dv_triage"]
        assert "## Generated RTL" in stage.prompt and "m_blk_csr" in stage.prompt and said in stage.prompt


# -- the test programs: prepare writes and builds them, simulate hands it the files a test could not read ------------------------------------------

FLOW_PREP = """name: t
steps:
  - {id: rtl, kind: rtl_gen, options: {rules: [rtl_rule.md]}}
  - {id: testplan, kind: dv_testplan, gate: auto, deps: []}
  - {id: tb, kind: dv_tb, gate: auto, deps: [rtl, testplan]}
  - {id: prepare, kind: dv_prepare, gate: auto, deps: [tb]}
  - {id: simulate, kind: dv_simulate, gate: auto, deps: [tb, prepare]}
"""


@pytest.fixture
def riscv(tmp_path):
    """A fake toolchain: gcc writes its -o file, spike prints a commit line, dtc is there."""
    d = tmp_path / "riscv" / "bin"
    d.mkdir(parents=True)
    scripts = {"riscv64-unknown-elf-gcc": '#!/bin/sh\nwhile [ $# -gt 0 ]; do if [ "$1" = -o ]; then shift; echo elf > "$1"; fi; shift; done\n',
               "spike": "#!/bin/sh\necho 'core   0: 0x80000000 (0x0000006f) j pc + 0x0'\n", "dtc": "#!/bin/sh\nexit 0\n"}
    for n, text in scripts.items():
        (d / n).write_text(text)
        (d / n).chmod(0o755)
    return d


def prep_engine(tmp_path, tools_dir, riscv, roles=True, monkeypatch=None) -> Engine:
    extra = (f"    riscv_gcc: {{cmd: '{riscv}/riscv64-unknown-elf-gcc {{args}}'}}\n    iss: {{cmd: '{riscv}/spike {{args}}'}}\n"
             "    sw_build: {cmd: 'make -C {src} PROG_DIR={out} RISCV_PREFIX={riscv_prefix} SPIKE={spike}'}\n") if roles else ""
    eng = make_engine(tmp_path, tools_dir, run_mode="noprog")
    cfg = (tmp_path / "q3tui.yaml").read_text()
    (tmp_path / "q3tui.yaml").write_text(cfg.replace("    run: ", extra + "    run: ", 1))
    (tmp_path / "flows" / "t.yaml").write_text(FLOW_PREP)
    return Engine(Project.open(tmp_path))


def test_prepare_builds_the_files_the_tests_read_and_the_tests_then_run(tmp_path, tools_dir, llm, riscv):
    eng = prep_engine(tmp_path, tools_dir, riscv)
    run_all(eng)
    run_step(eng, "prepare")
    (stage,) = [c for c in llm.calls if c.name == "prepare"]
    assert stage.write_dirs == [tmp_path / "src" / "sw"] and tmp_path / "src" / "rtl" in stage.deny_dirs and tmp_path / "build" in stage.deny_dirs
    assert [t.name for t in stage.sdk_tools] == ["build"] and "tb_top.sv" in stage.prompt and "RISCV_PREFIX=" in stage.prompt
    man = json.loads((tmp_path / "schemas" / "sw_manifest.json").read_text())
    assert man["built"] and man["prog_dir"] == "build/tb/programs" and [(t["tc_id"], t["status"]) for t in man["tests"]] == [("TC-002", "ready")]
    assert (tmp_path / "build/tb/programs/b.list").read_text().startswith("core")
    run_step(eng, "simulate")
    ev = {e["name"]: e["status"] for e in json.loads((tmp_path / "schemas" / "dv_evidence.json").read_text())["entries"]}
    assert ev == {"TC-001": "PASS", "TC-002": "PASS"}


def test_a_build_that_misses_a_file_gets_a_fix_round(tmp_path, tools_dir, llm, riscv):
    llm.vlsit_prepare_bad = 1
    eng = prep_engine(tmp_path, tools_dir, riscv)
    run_all(eng)
    run_step(eng, "prepare")
    fix = [c for c in llm.calls if c.name.startswith("prepare_fix")]
    assert len(fix) == 1 and "TC-002: b.list" in fix[0].prompt
    assert json.loads((tmp_path / "schemas" / "sw_manifest.json").read_text())["tests"][0]["status"] == "ready"


def test_without_the_tools_prepare_writes_the_sources_and_the_blocked_test_waits(tmp_path, tools_dir, llm, riscv, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("Q3TUI_HOME", str(tmp_path / "home"))
    eng = prep_engine(tmp_path, tools_dir, riscv, roles=False)
    run_all(eng)
    run_step(eng, "prepare")
    (stage,) = [c for c in llm.calls if c.name == "prepare"]
    assert not stage.sdk_tools and "no tool role 'sw_build'" in stage.prompt and (tmp_path / "src/sw/Makefile").is_file()
    man = json.loads((tmp_path / "schemas" / "sw_manifest.json").read_text())
    assert not man["built"] and man["tests"][0]["status"] == "not built" and any("RISC-V GCC" in p for p in man["problems"])
    run_step(eng, "simulate")                                               # TC-002 blocked; prepare cannot build: no change request
    assert not eng.state.feedback.get("prepare") and not eng.state.step_meta["simulate"].get("loop")


def test_a_blocked_test_goes_to_prepare_scoped_to_it_until_it_keeps_coming_back(tmp_path, tools_dir, llm, riscv):
    llm.vlsit_prepare_tests = []                                            # prepare built nothing the tests read
    llm.vlsit_prepare_bad = 99
    eng = prep_engine(tmp_path, tools_dir, riscv)
    run_all(eng)
    run_step(eng, "prepare")
    run_step(eng, "simulate")
    fb = eng.state.feedback["prepare"]
    assert len(fb) == 1 and fb[0].startswith("[prepare:TC-002] From the simulation (TC-002): the test could not read build/tb/programs/b.list")
    assert eng.state.step_meta["simulate"]["loop"] is True
    for _ in range(3):
        run_step(eng, "prepare")
        run_step(eng, "simulate")
    assert eng.state.step_meta["simulate"]["prepare_history"] == {"TC-002": 2}   # (pipeline.escalate_after_repeats: then a warning only)


OWNER = """name: owner
steps:
  - {id: rtl, kind: rtl_gen, options: {rules: [rtl_rule.md]}}
"""


def owner_flow(root: Path) -> Engine:
    """The RTL is the `owner` flow's (one rtl step): the tb option handoff_flow names it."""
    (root / "flows" / "owner.yaml").write_text(OWNER)
    cfg = (root / "q3tui.yaml").read_text()
    (root / "q3tui.yaml").write_text(cfg.replace("pipeline: {flow: t}", "pipeline: {flow: t, step_options: {tb: {handoff_flow: owner}}}", 1))
    return Engine(Project.open(root))


def run_events(root: Path) -> list[dict]:
    return [json.loads(l) for d in sorted((root / ".q3tui" / "runs").iterdir()) for l in (d / "events.jsonl").read_text().splitlines()]


RTL_BUG = [{"id": "TC-002", "verdict": "rtl_bug", "module": "leaf_b", "summary": "b late", "request": "register b"}]


def test_an_unattended_run_has_the_owner_flow_apply_an_rtl_fix_then_simulate_again_and_stops_when_the_rtl_did_not_change(tmp_path, tools_dir, llm):
    failing(tmp_path, tools_dir, llm, RTL_BUG)
    eng = owner_flow(tmp_path)
    eng.project.cfg.pipeline.auto_approve = True
    anyio.run(lambda: eng.run())
    ev = run_events(tmp_path)
    assert [e["step"] for e in ev if e["kind"] == "step_started"] == ["tb", "simulate", "rtl", "simulate"]  # the owner flow's rtl, in this run, then simulate again
    assert llm.vlsit_triage_calls == 2
    errors = [e["message"] for e in ev if e["kind"] == "error"]               # (the fake RTL step rewrote leaf_b as it was: no second owner run)
    assert any("the RTL did not change after the owner flow ran" in m and "run it (q3tui --flow owner run)" in m for m in errors)


def test_without_an_unattended_run_an_rtl_fix_waits_for_you_to_run_the_owner_flow(tmp_path, tools_dir, llm):
    failing(tmp_path, tools_dir, llm, RTL_BUG)
    eng = owner_flow(tmp_path)
    anyio.run(lambda: eng.run())
    ev = run_events(tmp_path)
    assert "rtl" not in [e["step"] for e in ev if e["kind"] == "step_started"] and eng.state.feedback.get("rtl")
    assert any("run it (q3tui --flow owner run)" in e["message"] for e in ev if e["kind"] == "error")


def test_a_fix_loop_under_auto_answer_does_not_approve_a_step_that_went_pending(tmp_path, tools_dir, llm):
    eng = failing(tmp_path, tools_dir, llm, [{"id": "TC-002", "verdict": "tb_bug", "summary": "wrong value", "request": "use the spec's value"},
                                             {"id": "TC-002", "verdict": "rtl_bug", "module": "leaf_b", "summary": "b late", "request": "register b"},
                                             {"id": "TC-002", "verdict": "spec_issue", "summary": "the spec is silent", "request": "assume one cycle"}])
    eng = owner_flow(tmp_path)
    eng.project.cfg.pipeline.auto_answer = True                             # (rv32iv: fixes to tb and the rtl flow, a blocking question, auto-answer)
    anyio.run(lambda: eng.run())
    errors = [json.loads(l) for d in sorted((tmp_path / ".q3tui" / "runs").iterdir()) for l in (d / "events.jsonl").read_text().splitlines()]
    assert not [e for e in errors if e["kind"] == "error" and "cannot approve" in e.get("message", "")]
    assert llm.vlsit_triage_calls >= 2                                      # (the loop updated tb and ran simulate again)


def test_the_toolchain_is_found_by_role_or_by_a_search_and_set_up_into_tools_json(tmp_path, riscv, monkeypatch):
    from q3tui.flows.dv.prepare import toolchain

    monkeypatch.setenv("PATH", f"{riscv}:/usr/bin:/bin")
    tc = toolchain.find(None)
    assert tc.gcc == str(riscv / "riscv64-unknown-elf-gcc") and tc.prefix == str(riscv / "riscv64-unknown-elf-") and tc.spike and tc.dtc and tc.ready
    tools_json = tmp_path / "config" / "tools.json"
    tools_json.parent.mkdir()
    tools_json.write_text(json.dumps({"roles": {"iss": {"cmd": "mine {args}"}}}))
    assert toolchain.write_roles(tools_json, toolchain.roles_for(tc)) == ["riscv_gcc", "sw_build"]  # (yours is kept)
    roles = json.loads(tools_json.read_text())["roles"]
    assert roles["iss"]["cmd"] == "mine {args}" and str(riscv) in roles["sw_build"]["env"]["PATH"]
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("Q3TUI_HOME", str(tmp_path / "home"))
    none = toolchain.find(None)
    steps = [w for w, _, _ in toolchain.install_steps(none, tmp_path / "home" / "tools")]
    assert not none.ready and steps == ["GCC: download", "GCC: unpack", "dtc: clone", "dtc: build", "Spike: clone", "Spike: build", "riscv-tests: clone"]
