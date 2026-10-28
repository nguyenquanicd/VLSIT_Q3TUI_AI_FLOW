"""The rtl flow's generator (flows/rtl/rtl/gen.py, the base of its `rtl` step): fake LLM stages, fake tools (scripts standing in
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
from q3tui.flows.rtl.rtl.plan import RtlPlan, PlanModule, levels, plan_from_spec
from q3tui.flows.rtl.rtl.gen import VModuleResult
from q3tui.steps import register_kind
from tests.fakes import FakeLLM
from tests.vlsit_fakes import register

# -- fake tools -----------------------------------------------------------------------------------------------

LINT = '''
import sys
files = [l.strip() for l in open(sys.argv[1]) if l.strip()]
warns = 0
for f in files:
    text = open(f).read()
    if "LINT_WARN" in text:
        print(f"%Warning-UNUSED: {f}:3:1: Signal is not used: 'x'")
        warns += 1
    if "LINT_ERR" in text:
        print(f"%Error: {f}:4:1: syntax error")
        print("%Error: Exiting due to 1 error(s)")
        sys.exit(1)
if warns:  # like `verilator -Wall`: warnings alone fail the run, with a summary line in no file
    print(f"%Error: Exiting due to {warns} warning(s)")
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
    return None


MODULES = {
    "leaf_a": {"reqs": ["REQ-001"], "children": []},
    "leaf_b": {"reqs": ["REQ-002"], "children": []},
    "top": {"reqs": ["REQ-003"], "children": ["leaf_a", "leaf_b"]},
}
REQS = {"REQ-001": "a is registered", "REQ-002": "b is registered", "REQ-003": "top ties them"}

register_kind("rtl_gen", "q3tui.flows.rtl.rtl.gen:RtlGenStep")
FLOW = """name: t
steps:
  - {id: rtl, kind: rtl_gen, options: {rules: [rtl_rule.md]}}
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
    msgs = "\n".join(str(v) for v in rules_check.check_source(src, "m_x", rules_check.profile_for(["rtl_rule_default.md"])))
    assert "plain `always`" in msgs and "positional" in msgs
    uni = rules_check.profile_for([])
    assert uni.name == "universal"
    assert [v.rule for v in rules_check.check_source("module x; logic a; initial a = #3 1; endmodule", "x", uni)] == ["P1", "P2"]
    assert rules_check.req_tags("// REQ-001, REQ-12x REQ-0002") == {"REQ-001", "REQ-0002"}


def test_rule_checker_enum_member_by_ternary_needs_a_cast():
    """ENM-06: Icarus rejects an enum member chosen by `? :` ("This assignment requires an explicit cast"); Verilator does not."""
    src = textwrap.dedent("""\
        module m_x (input logic i_a, output logic o_b);
          typedef enum logic [1:0] {ENUM_ST_IDLE = 2'd0, ENUM_ST_RUN = 2'd1} st_type_def;
          st_type_def w_nxt, w_cast, w_ok;
          always_comb begin : p_com_nxt
            w_nxt = i_a ? ENUM_ST_RUN : ENUM_ST_IDLE;
            w_cast = st_type_def'(i_a ? ENUM_ST_RUN : ENUM_ST_IDLE);
            if (i_a) w_ok = ENUM_ST_RUN; else w_ok = ENUM_ST_IDLE;
            o_b = (w_nxt == ENUM_ST_RUN) ? 1'b1 : 1'b0;
          end
        endmodule
        """)
    assert rules_check.enum_ternaries(rules_check.strip_comments(src)) == [5]
    assert any(v.rule == "ENM-06" and v.line == 5 for v in rules_check.check_source(src, "m_x", rules_check.profile_for([])))


def test_icarus_sorry_is_a_note_and_compile_errors_are_listed_once():
    from q3tui.eda.base import ToolResult
    from q3tui.eda.tools import parse_iverilog
    from q3tui.flows.rtl.lib import smoke

    log = "".join(f"src/rtl/m_lsu.sv:153: sorry: constant selects in always_* processes are not currently supported (all bits will be included).\n"
                  for _ in range(30)) + "src/rtl/m_vpu_dec.sv:396: error: This assignment requires an explicit cast.\n" * 2
    diags = parse_iverilog(log)
    assert {d.severity for d in diags if "sorry" in d.code.lower()} == {"warning"}
    res = ToolResult("sim", "iverilog", False, returncode=1, diagnostics=diags)
    assert smoke.compile_errors(res) == ["src/rtl/m_vpu_dec.sv:396 This assignment requires an explicit cast."]


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
    assert "Chip area" in (tmp_path / "build" / "sim" / "rtl" / "synth_default.log").read_text()


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
    # the spec parser rewords the descriptions of every module: not a change (no LLM stage); with a real change the new wording comes along
    data = artifacts.read(project, "structured_spec.json")
    for m in data["modules"]:
        m["description"] = "reworded: " + m["description"]
    artifacts.write(project, "structured_spec.json", data, check=False)
    n = len(llm.calls)
    run_step(eng, "rtl")
    assert len(llm.calls) == n
    data["requirements"][1]["text"] = "b is registered three times"
    artifacts.write(project, "structured_spec.json", data, check=False)
    run_step(eng, "rtl")
    new = llm.calls[n:]
    assert [c.name for c in new] == ["rtl_leaf_b"] and "the module's description is now: reworded: the leaf_b" in new[0].prompt
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


def test_rtl_finished_modules_are_saved_before_the_step_ends(tmp_path, tools_dir, llm, monkeypatch):
    """A killed run (no cancel path, nothing saved at the end) keeps the modules it finished: the next run skips them."""
    eng = make_engine(tmp_path, tools_dir)
    on_disk = {}

    async def spy(stage, cfg, emit):
        if stage.name == "rtl_top":  # the leaves are done: what a kill right now would leave in pipeline.json
            on_disk.update(json.loads(eng.state_path.read_text())["step_meta"]["rtl"]["modules"])
        return await llm(stage, cfg, emit)

    monkeypatch.setattr(runtime, "run_stage", spy)
    run_step(eng, "rtl")
    assert {"leaf_a", "leaf_b"} <= set(on_disk)


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


def test_a_lint_tool_that_fails_without_a_diagnostic_stops_the_step_without_fix_rounds(tmp_path, tools_dir, llm):
    from q3tui.pipeline.base import StepFailed

    eng = make_engine(tmp_path, tools_dir)
    (tools_dir / "lint.py").write_text("import sys\nprint('ld: cannot find -lint=all')\nsys.exit(2)\n")
    with pytest.raises(StepFailed) as exc:
        run_step(eng, "rtl")
    assert "`lint` tool exited with rc=2" in str(exc.value) and "not of the design" in str(exc.value) and "cannot find -lint=all" in str(exc.value)
    assert not [c for c in llm.calls if c.name.startswith("rtl_fix")]  # nothing was "fixed"
    assert eng.state.steps["rtl"].status == "failed"


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
