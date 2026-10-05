"""The VLSIT flow's `rtl` and `tb` steps (steps/vlsit/rtl.py, tb.py): fake LLM stages, fake tools (scripts standing in
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
from q3tui.steps.vlsit import artifacts, rules_check, synth
from q3tui.flows.vlsit.tb import gen as tb_gen
from q3tui.flows.vlsit.tb import plan as tb_plan
from q3tui.flows.vlsit.rtl.plan import RtlPlan, PlanModule, levels, plan_from_spec
from q3tui.flows.vlsit.rtl.step import VModuleResult
from q3tui.flows.vlsit.tb.plan import TbCase, TbPlan, TbUnitResult
from tests.fakes import FakeLLM
from tests.vlsit_fakes import register

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
  - {id: rtl, kind: vlsit_rtl, options: {rules: [rtl_rule.md]}}
  - {id: tb, kind: vlsit_tb, gate: auto, deps: [rtl]}
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

def test_rule_checker_project_profile():
    src = _rtl_text(type("F", (), {"vlsit_modules": MODULES})(), "leaf_a", None)
    prof = rules_check.profile_for(["rtl_rule.md"])
    assert rules_check.check_source(src, "leaf_a", prof) == []
    bad = src.replace("reg_q", "q").replace("i_resetn_core", "i_rst_n_core") + "\n  initial begin end\n"
    msgs = [str(v) for v in rules_check.check_source(bad, "leaf_a", prof)]
    assert any("initial" in m for m in msgs) and any("i_rst_n_core" in m for m in msgs) and any("`q`" in m for m in msgs)
    assert any("does not match" in str(v) for v in rules_check.check_source(src, "wrong_name", prof)) or \
        any("R9" in str(v) for v in rules_check.check_source(src, "wrong_name", prof))


def test_rule_checker_default_profile_and_universal():
    src = "module m_x (input logic i_clk_core, input logic i_rst_n_core, output logic o_y);\n  always @(*) o_y = 1'b1;\n  m_y u_y (i_clk_core);\nendmodule\n"
    msgs = "\n".join(str(v) for v in rules_check.check_source(src, "m_x", rules_check.profile_for(["vlsit_rtl_rule_default.md"])))
    assert "plain `always`" in msgs and "positional" in msgs
    uni = rules_check.profile_for([])
    assert uni.name == "universal"
    assert [v.rule for v in rules_check.check_source("module x; logic a; initial a = #3 1; endmodule", "x", uni)] == ["P1", "P2"]
    assert rules_check.req_tags("// REQ-001, REQ-12x REQ-0002") == {"REQ-001", "REQ-0002"}


def test_plan_from_spec_and_levels():
    plan = plan_from_spec(spec_json())
    assert plan.top == "top" and [[m.name for m in lv] for lv in levels(plan)] == [["leaf_a", "leaf_b"], ["top"]]
    only_reqs = {"metadata": {"ip_name": "x"}, "requirements": [
        {"req_id": "REQ-001", "text": "t", "rtl_modules": ["x_top", "x_sub"]}, {"req_id": "REQ-002", "text": "t", "rtl_modules": ["x_sub"]}]}
    p2 = plan_from_spec(only_reqs)
    assert {m.name: m.req_ids for m in p2.modules} == {"x_top": ["REQ-001"], "x_sub": ["REQ-001", "REQ-002"]}
    assert plan_from_spec({"requirements": [{"req_id": "REQ-001", "text": "t"}]}) is None
    with pytest.raises(ValueError):
        levels(RtlPlan(top="a", modules=[PlanModule(name="a", instances=["b"]), PlanModule(name="b", instances=["a"])]))


def test_synth_helpers():
    text = synth.script_text([Path("/a.sv"), Path("/b.sv")], "top", "/lib.lib")
    assert "read_verilog -sv /a.sv" in text and "dfflibmap -liberty /lib.lib" in text and "hierarchy -check -top top" in text
    assert "abc\n" in synth.script_text([Path("/a.sv")], "top")
    log = "Yosys 0.58+35 (git)\n   Number of cells:   17\n   Chip area for module '\\top': 99.5\n"
    assert synth.parse_stat(log, "top") == {"cell_count": 17, "area_um2": 99.5, "tool": "Yosys 0.58+35"}
    assert synth.parse_stat("     12 cells\n")["cell_count"] == 12
    assert synth.attribute_errors("ERROR: src/rtl/leaf_a.sv:3: bad\nERROR: other", ["leaf_a", "top"]) == {
        "leaf_a": ["ERROR: src/rtl/leaf_a.sv:3: bad"], "": ["ERROR: other"]}
    assert synth.corners({"synth": {"corners": [{"name": "tt", "liberty": "~/x.lib"}]}})[0]["name"] == "tt"


# -- rtl step ----------------------------------------------------------------------------------------------------

def test_rtl_happy_path(tmp_path, tools_dir, llm):
    eng = make_engine(tmp_path, tools_dir)
    run_step(eng, "rtl")
    rtl = tmp_path / "src" / "rtl"
    assert sorted(p.name for p in rtl.glob("*.sv")) == ["leaf_a.sv", "leaf_b.sv", "top.sv"]
    assert (rtl / "filelist.f").read_text().split() == ["src/rtl/leaf_a.sv", "src/rtl/leaf_b.sv", "src/rtl/top.sv"]  # leaves first
    assert eng.state.steps["rtl"].status == "done"
    report = json.loads((tmp_path / "schemas" / "synth_report.json").read_text())
    assert report["synthesis"] == "pass" and report["tool"] == "Yosys 0.58" and report["top_module"] == "top"
    assert report["corners"]["default"]["cell_count"] == 42 and report["corners"]["default"]["area_estimate_um2"] == 1234.5
    assert report["lint"]["status"] == "pass" and report["lint"]["warnings"] == 0
    assert report["modules"]["leaf_a"]["req_ids"] == ["REQ-001"] and report["req_ids_distinct"] == 3 and report["req_ids_tagged"] >= 6
    assert report["modules"]["top"]["lint"] == "pass" and report["rule_profile"] == "project"
    # the stage: only its own file writable, never the testbench / SVA (independence), facts in the prompt
    c = calls(llm, "rtl_top")[0]
    assert c.write_files == [rtl / "top.sv"] and (tmp_path / "src" / "tb") in c.deny_dirs and (tmp_path / "src" / "sva") in c.deny_dirs
    assert "REQ-003: top ties them" in c.prompt and "PR_W = 8" in c.prompt and "src/rtl/leaf_a.sv" in c.prompt
    assert "Quy tắc RTL" in c.system_prompt and "PR_[A-Z0-9_]+" not in c.system_prompt  # the rule text, not the regexes
    assert [x.name for x in llm.calls][:2] in (["rtl_leaf_a", "rtl_leaf_b"], ["rtl_leaf_b", "rtl_leaf_a"])  # children before the top
    assert "Chip area" in (tmp_path / ".q3tui" / "sim" / "rtl" / "synth_default.log").read_text()


def test_rtl_rule_violation_goes_to_a_fresh_fix_session(tmp_path, tools_dir, llm):
    llm.vlsit_bad = {"leaf_a": ["rule"]}
    eng = make_engine(tmp_path, tools_dir)
    run_step(eng, "rtl")
    cs = calls(llm, "rtl_leaf_a")
    assert len(cs) == 2 and cs[1].resume is None
    assert "initial block" in cs[1].prompt and "initial begin end" in cs[1].prompt  # the problem and the current file
    assert "initial" not in (tmp_path / "src/rtl/leaf_a.sv").read_text()


def test_rtl_lint_warning_must_be_zero(tmp_path, tools_dir, llm):
    llm.vlsit_bad = {"leaf_b": ["lint"]}
    eng = make_engine(tmp_path, tools_dir)
    run_step(eng, "rtl")
    cs = calls(llm, "rtl_leaf_b")
    assert len(cs) == 2 and "lint warning UNUSED" in cs[1].prompt
    # lint_max_warnings allows some
    (tmp_path / "b").mkdir()
    eng2 = make_engine(tmp_path / "b", tools_dir)
    eng2.by_name["rtl"].options["lint_max_warnings"] = 1
    llm.vlsit_bad = {"leaf_b": ["lint"]}
    run_step(eng2, "rtl")
    assert len(calls(llm, "rtl_leaf_b")) == len(cs) + 1  # one more write, no fix round


def test_rtl_missing_tag_is_a_problem(tmp_path, tools_dir, llm):
    eng = make_engine(tmp_path, tools_dir)
    llm.vlsit_modules["leaf_a"]["reqs"] = ["REQ-001"]
    run_step(eng, "rtl")
    f = tmp_path / "src/rtl/leaf_a.sv"
    f.write_text(f.read_text().replace("REQ-001", "REQ-9"))
    n = len(llm.calls)
    run_step(eng, "rtl")  # (the hand edit is found by the checks and repaired: only that module)
    new = llm.calls[n:]
    assert [c.name for c in new] == ["rtl_leaf_a"] and "REQ trace tags missing: REQ-001" in new[0].prompt


def test_rtl_unchanged_rerun_uses_no_llm_and_updates_are_targeted(tmp_path, tools_dir, llm):
    eng = make_engine(tmp_path, tools_dir)
    run_step(eng, "rtl")
    n = len(llm.calls)
    run_step(eng, "rtl")
    assert len(llm.calls) == n  # nothing changed: only the checks ran
    # one requirement changed: only its module is updated (an update prompt, not a rewrite); the top's child file is identical
    project = Project.open(tmp_path)
    data = artifacts.read(project, "structured_spec.json")
    data["requirements"][1]["text"] = "b is registered twice"
    artifacts.write(project, "structured_spec.json", data, check=False)
    run_step(eng, "rtl")
    new = llm.calls[n:]
    assert [c.name for c in new] == ["rtl_leaf_b"]
    assert "REQ-002 changed: b is registered twice (was: b is registered)" in new[0].prompt and "cập nhật" in new[0].prompt
    # a scoped change request reaches only that module
    n = len(llm.calls)
    eng.request_change("rtl", "[rtl:leaf_a] use a wider register")
    run_step(eng, "rtl")
    new = llm.calls[n:]
    assert [c.name for c in new] == ["rtl_leaf_a"] and "use a wider register" in new[0].prompt
    # a change in the configuration (a parameter) updates every module
    n = len(llm.calls)
    artifacts.write(project, "final_config.json", config_json(PR_W=16), check=False)
    run_step(eng, "rtl")
    assert {c.name for c in llm.calls[n:]} == {"rtl_leaf_a", "rtl_leaf_b", "rtl_top"}


def test_rtl_synthesis_not_run_is_never_passed(tmp_path, tools_dir, llm):
    eng = make_engine(tmp_path, tools_dir, synth_role=False)
    run_step(eng, "rtl")
    report = json.loads((tmp_path / "schemas" / "synth_report.json").read_text())
    assert report["synthesis"] == "not run" and report["tool"] == "not run" and "no tool for role 'synth'" in report["note"]
    assert report["corners"] == {}


def test_rtl_synthesis_error_is_attributed_and_fixed(tmp_path, tools_dir, llm):
    llm.vlsit_bad = {"leaf_b": ["synth"]}
    eng = make_engine(tmp_path, tools_dir)
    run_step(eng, "rtl")
    cs = calls(llm, "rtl_leaf_b")
    assert len(cs) == 2 and "synthesis: ERROR" in cs[1].prompt
    assert json.loads((tmp_path / "schemas" / "synth_report.json").read_text())["synthesis"] == "pass"


def test_rtl_gives_up_naming_what_is_left(tmp_path, tools_dir, llm):
    llm.vlsit_always_bad = {"leaf_a": "rule"}
    eng = make_engine(tmp_path, tools_dir, extra_cfg="rtl: {max_fix_attempts: 1}\n")
    with pytest.raises(StepFailed) as exc:
        run_step(eng, "rtl")
    assert "leaf_a" in str(exc.value) and "initial" in str(exc.value)
    assert len(calls(llm, "rtl_leaf_a")) == 2  # one write + one fix round
    assert eng.state.steps["rtl"].status == "failed"


def test_rtl_missing_lint_role_waits_naming_the_role(tmp_path, tools_dir, llm):
    eng = make_engine(tmp_path, tools_dir, lint_role=False)
    view = eng.evaluate(eng.by_name["rtl"])
    assert view.status == "missing_input" and "role 'lint'" in view.detail


def test_rtl_plans_modules_itself_when_the_spec_has_none(tmp_path, tools_dir, llm):
    eng = make_engine(tmp_path, tools_dir, spec=spec_json(with_modules=False))
    run_step(eng, "rtl")
    assert llm.vlsit_plan_calls == 1 and (tmp_path / "schemas" / "rtl_plan.json").is_file()
    assert len(list((tmp_path / "src/rtl").glob("*.sv"))) == 3
    run_step(eng, "rtl")  # the plan is kept: no planning again
    assert llm.vlsit_plan_calls == 1


def test_rtl_questions_are_kept_per_module(tmp_path, tools_dir, llm):
    from q3tui.steps.common import Question

    llm.vlsit_questions = {"leaf_a": [Question(id="", question="Reset value of the register?", blocking=False,
                                                default_assumption="zero", kind="req_gap")]}
    eng = make_engine(tmp_path, tools_dir)
    run_step(eng, "rtl")
    qs = eng.by_name["rtl"].question_list(eng)
    assert [q["question"] for q in qs] == ["Reset value of the register?"] and qs[0]["id"].startswith("Q-R")
    assert eng.by_name["rtl"].answer(eng, qs[0]["id"], "zero")
    # the settled answer reaches the module as input (the answers file is an input: it is stale)
    assert eng.evaluate(eng.by_name["rtl"]).status == "stale"


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


def test_tb_and_rtl_steps_declare_their_tools():
    from q3tui.flows import load_flow
    from q3tui.eda.tools import roles_needed

    assert set(roles_needed(load_flow("vlsit"))) >= {"lint", "synth", "sim", "run"}


# -- a tool that fails by itself is not the design's fault (real run: `vcs -lint=all` made every lint exit 2 with no
#    diagnostic; the step spent ~$2.7 of fix rounds rewriting correct RTL) ----------------------------------------------

def test_a_lint_tool_that_fails_without_a_diagnostic_stops_the_step_without_fix_rounds(tmp_path, tools_dir, llm):
    from q3tui.pipeline.base import StepFailed

    eng = make_engine(tmp_path, tools_dir)
    (tools_dir / "lint.py").write_text("import sys\nprint('ld: cannot find -lint=all')\nsys.exit(2)\n")
    with pytest.raises(StepFailed) as exc:
        run_step(eng, "rtl")
    assert "`lint` tool exited with rc=2" in str(exc.value) and "not of the design" in str(exc.value) and "cannot find -lint=all" in str(exc.value)
    assert not [c for c in llm.calls if c.name.startswith("rtl_fix")]  # nothing was "fixed"
    assert eng.state.steps["rtl"].status == "failed"


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


def test_design_compiler_without_a_library_is_not_run_and_its_setup_errors_are_not_rtl_errors(tmp_path, tools_dir, llm):
    from q3tui.pipeline.base import StepFailed

    eng = make_engine(tmp_path, tools_dir)
    (tmp_path / "q3tui.yaml").write_text((tmp_path / "q3tui.yaml").read_text().replace("parse: yosys", "parse: dc"))
    eng = Engine(Project.open(tmp_path))
    run_step(eng, "rtl")  # no library in options.synth.corners: synthesis is "not run", never "pass"
    report = json.loads((tmp_path / "schemas" / "synth_report.json").read_text())
    assert report["synthesis"] == "not run" and "target library" in report["note"]
    # with a library, a setup error that names none of our modules is the tool's problem
    flow = (tmp_path / "flows" / "t.yaml").read_text()
    (tmp_path / "flows" / "t.yaml").write_text(flow.replace("options: {rules: [rtl_rule.md]}",
                                                            "options: {rules: [rtl_rule.md], synth: {corners: [{name: tt, library: /x.db}]}}"))
    (tools_dir / "synth.py").write_text("import sys\nprint('Error: No target library found. (OPT-1312)')\nsys.exit(0)\n")
    eng = Engine(Project.open(tmp_path))
    eng.reset("rtl")
    with pytest.raises(StepFailed) as exc:
        run_step(eng, "rtl")
    assert "OPT-1312" in str(exc.value) and "no fix round was spent" in str(exc.value)
