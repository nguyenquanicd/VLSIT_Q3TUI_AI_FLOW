"""The RTL flow (docs/spec/rtl-flow.md): the flow file, the rule stack, Verible (flags, log parsing, format check), `rtl_dir`.
No live LLM and no EDA tool: fake tools are small Python scripts."""

from pathlib import Path

import pytest

from q3tui import flows
from q3tui.core.project import Project
from q3tui.eda import tools as T
from q3tui.flows.rtl.lib import rules, verible
from q3tui.pipeline.engine import Engine

LOG = """rtl/m_a.sv:3:8: Module name 'M_A' must be lower_snake_case. [Style: module-filename] [module-filename]
rtl/m_a.sv:7:1-4: Remove trailing spaces. [Style: trailing-spaces] [no-trailing-spaces]
rtl/m_b.sv:2:5: syntax error at token "endmodul"
some unrelated line
"""


def test_flow_loads_and_is_valid():
    flow = flows.load_flow("rtl")
    assert [s.id for s in flow.steps][:9] == ["spec", "parse", "config", "rules", "csr", "rtl", "format", "style_lint", "smoke"]
    assert flows.validate(flow) == []
    assert flow.session == "fresh"
    assert {"verible_lint", "verible_format", "csr_gen"} <= set(flow.tool_defaults)  # the flow's own roles (tools.json)


def test_the_default_profile_accepts_what_the_rule_text_allows():
    """TYP-02 / P20 (`input wire`) and ARI-04 / P21 (`for (int idx …)`) are allowed by the rule text: the code check must not
    flag them, and the rules step shows the judge what each check means (a run read the raw flags as a contradiction)."""
    from q3tui.flows.rtl.lib import rules_check

    src = ("module m_x (\n  input wire i_clk_core,\n  input wire i_rst_n_core,\n  input wire [3:0] i_a,\n  output logic [3:0] o_y\n);\n"
           "  logic [3:0] w_t;\n  always_comb begin : p_com_t\n    w_t = '0;\n    for (int idx = 0; idx < 4; idx++) begin\n"
           "      w_t[idx] = i_a[idx];\n    end\n  end\n  assign o_y = w_t;\nendmodule\n")
    prof = rules_check.profile_for(["rtl_rule_default.md"])
    assert rules_check.check_source(src, "m_x", prof) == []
    bad = src.replace("logic [3:0] w_t;", "wire [3:0] w_t;\n  int w_n;")
    assert {v.rule for v in rules_check.check_source(bad, "m_x", prof)} >= {"R7/P20", "P21"}  # (internal wire / int stay forbidden)
    text = " ".join(rules_check.describe(prof).values())
    assert "`input wire i_a`) is not checked and is accepted" in text and "for-header loop index" in text and "forbid_wire_reg" not in text


def test_rule_stack_puts_your_file_first(tmp_path):
    project = Project.open(tmp_path)
    assert rules.stack(project, ["rtl_rule_default.md"]) == ["rtl_rule_default.md"]
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / rules.USER_RULES).write_text("- **U-01** mine\n")
    assert rules.stack(project, ["rtl_rule_default.md"]) == ["config/rtl_rules.md", "rtl_rule_default.md"]
    assert rules.stack(None, ["x.md"]) == ["x.md"]
    # the same names are what the generator reads (project path first, then the shipped file)
    from q3tui.flows.spec.lib.base import rules_text

    text = rules_text(rules.stack(project, ["rtl_rule_default.md"]), tmp_path)
    assert text.index("rtl_rules.md") < text.index("rtl_rule_default.md") and "U-01" in text


def test_parse_verible():
    d = T.parse_log("verible", LOG)
    assert [(x.severity, x.code, x.file, x.line) for x in d] == [
        ("warning", "module-filename", "rtl/m_a.sv", 3), ("warning", "no-trailing-spaces", "rtl/m_a.sv", 7), ("error", "SYNTAX", "rtl/m_b.sv", 2)]
    assert "[Style" not in d[0].message


REAL_LINT = """Bad_Mod.sv:2:13-17: Explicitly define a storage type for every parameter and localparam, (Width). [Style: constants] [explicit-parameter-storage-type]
Bad_Mod.sv:3:1: Use spaces, not tabs. [Style: tabs] [no-tabs]
syn.sv:3:1-9: syntax error at token "endmodule"
"""


def test_parse_verible_real_output():  # (captured from verible v0.0-4296-g0f262651)
    d = T.parse_log("verible", REAL_LINT)
    assert [(x.severity, x.code, x.file, x.line) for x in d] == [("warning", "explicit-parameter-storage-type", "Bad_Mod.sv", 2), ("warning", "no-tabs", "Bad_Mod.sv", 3),
                                                              ("error", "SYNTAX", "syn.sv", 3)]
    # the formatter exits 0 on a syntax error and prints it with a `file: ` prefix
    f = T.parse_log("verible", 's2.sv: s2.sv:3:1-9: syntax error at token "endmodule"\n')
    assert [(x.severity, x.file, x.line) for x in f] == [("error", "s2.sv", 3)]
    # the tool refusing its own flags: an error with no file (exit code 1 for a bad rule, 2 for a value out of range)
    t = T.parse_log("verible", "ERROR: Illegal value '+bogus-rule' specified for flag 'rules'; [ERR] Invalid flag \"bogus-rule\"\n"
                               "E1019 01:21:19.506875 2024362 verilog-linter.cc:148] Fatal error: line-length length: 20 out of range [40...1000]\n"
                               "E1019 19:03:47.772217  727747 verilog-linter.cc:148] Fatal error: the pid is padded with two spaces\n")
    assert [(x.severity, x.code, x.file) for x in t] == [("error", "TOOL", None)] * 3 and "out of range" in t[1].message and "padded" in t[2].message


def test_verible_findings_carry_the_source_line(tmp_path):
    (tmp_path / "src" / "rtl").mkdir(parents=True)
    (tmp_path / "src" / "rtl" / "m_a.sv").write_text("module M_A;\nendmodule\n\n  wire x;   \n")
    diags = T.parse_log("verible", "src/rtl/m_a.sv:4:9: Remove trailing spaces. [Style: trailing-spaces] [no-trailing-spaces]\n")
    f = verible.findings(diags, tmp_path)
    assert [(x.module, x.line, x.rule, x.text) for x in f] == [("m_a", 4, "no-trailing-spaces", "  wire x;")]
    assert f[0].key() == "m_a:4:no-trailing-spaces"


def test_flags_validation():
    ok = ["--ruleset=default", "--rules=+line-length=length:100,-no-tabs,module-filename"]
    assert verible.validate_flags(ok) == []
    assert verible.split_rules(ok[1]) == ["+line-length=length:100", "-no-tabs", "module-filename"]
    bad = verible.validate_flags(["rules=x", "--lint_fatal", "--rules=+no-tabs,-no-tabs"])
    assert any("not a flag" in p for p in bad) and any("not allowed" in p for p in bad) and any("both on and off" in p for p in bad)


HELP = """always-comb                        Checks that there are no occurrences of `always @*`. Use `always_comb` instead.
                                   Enabled by default: true

banned-declared-name-patterns      Checks for banned declared name against set of unwanted patterns.
                                   Enabled by default: false

line-length                        Checks that all lines do not exceed the maximum allowed length.
                                   Parameter:
                                       * `length` Default: `100` Desired line length
                                   Enabled by default: true

parameter-name-style               Checks that parameter names conform to a naming convention.
                                   Parameters:
                                       * `localparam_style` Default: `CamelCase` Style of localparam names
                                       * `parameter_style_regex` Default: `` A regex used to check parameter name style.
                                   Enabled by default: true
"""


def test_rule_catalog_from_help_rules():  # (the text is what verible v0.0-4296 prints)
    cat = verible.rule_catalog(HELP)
    assert cat.splitlines() == [
        "- always-comb* — Checks that there are no occurrences of `always @*`. Use `always_comb` instead.",
        "- banned-declared-name-patterns — Checks for banned declared name against set of unwanted patterns.",
        "- line-length* — Checks that all lines do not exceed the maximum allowed length. [length=100]",
        '- parameter-name-style* — Checks that parameter names conform to a naming convention. [localparam_style=CamelCase, parameter_style_regex=""]']
    assert verible.rule_catalog("nothing like it") == ""


def test_format_may_only_change_whitespace():
    assert verible.same_code("module m;\n  wire  x;\nendmodule\n", "module m; wire x;\n endmodule")
    assert not verible.same_code("module m; wire x; endmodule", "module m; wire y; endmodule")


def test_rtl_dir_option_moves_the_rtl(tmp_path):
    proj = Project.open(tmp_path)
    engine = Engine(proj, flow=flows.load_flow("rtl", tmp_path))
    rtl = engine.by_name["rtl"]
    assert rtl.lay(engine).rtl == tmp_path / "src" / "rtl"  # (project.rtl_dir)


# -- the steps with a fake Verible and a fake LLM -------------------------------------------------------------------

import sys

import anyio

from q3tui.llm import runtime
from q3tui.llm.runtime import StageResult
from q3tui.pipeline.base import StepFailed
from q3tui.flows.rtl.lib.models import Behaviour, Fix, RuleFinding, RulesResult, SmokePlan, SmokeTriage, StyleVerdict, Verdict, Waiver
from q3tui.flows.rtl.rules.step import diff_rules, parse_rules

FAKE_VERIBLE = '''
import re, sys
args = sys.argv[1:]
flags = [a for a in args if a.startswith("--")]
files = [a for a in args if not a.startswith("--")]
if any(a.startswith("--help_rules") for a in flags):
    print("line-length                        Checks the line length.\\n                                   Parameter:\\n                                       * `length` Default: `100` Desired line length\\n                                   Enabled by default: true\\n\\nno-tabs                            No tabs.\\n                                   Enabled by default: true\\n\\nparameter-name-style               Parameter names.")
    sys.exit(0)
if any("bogus-rule" in a for a in flags):
    print("ERROR: Illegal value '+bogus-rule' specified for flag 'rules'; [ERR] Invalid flag \\"bogus-rule\\"")
    sys.exit(1)
inplace = "--inplace" in flags
bad = 0
for f in files:
    text = open(f).read()
    if "SYNTAX" in text:
        print(f'{f}: {f}:1:1: syntax error at token "x"' if inplace else f'{f}:1:1: syntax error at token "x"')  # (the formatter: prefix, exit 0)
        bad = 0 if inplace else 1
        continue
    if inplace:
        out = "\\n".join(re.sub(r"[ \\t]+", " ", ln).rstrip() for ln in text.splitlines()) + "\\n"
        if "CORRUPT" in text:
            out = out.replace("wire x", "wire y")
        open(f, "w").write(out)
        continue
    for i, line in enumerate(text.splitlines(), 1):
        if "BAD_NAME" in line:
            print(f"{f}:{i}:3: Parameter name does not match. [Style: constants] [parameter-name-style]")
            bad = 1
        if line.rstrip() != line:
            print(f"{f}:{i}:9: Remove trailing spaces. [Style: trailing-spaces] [no-trailing-spaces]")
            bad = 1
sys.exit(bad)
'''


class ScriptedLLM:
    """Stage name → a function(stage) → the output model; records every stage."""

    def __init__(self):
        self.calls = []
        self.answers = {}

    async def __call__(self, stage, cfg, emit):
        self.calls.append(stage)
        out = self.answers[stage.name](stage) if stage.name in self.answers else None
        emit.add_cost(0.01)
        return StageResult("ok", out, 0.01, 1, "s")

    def names(self):
        return [c.name for c in self.calls]


@pytest.fixture
def rtl_proj(tmp_path, monkeypatch):
    py = sys.executable
    (tmp_path / "fake_verible.py").write_text(FAKE_VERIBLE)
    (tmp_path / "q3tui.yaml").write_text(
        "pipeline: {flow: rtl}\ntools:\n  roles:\n"
        f"    verible_lint: {{cmd: '{py} {tmp_path}/fake_verible.py {{args}} {{files}}', parse: verible, ok_returncodes: [0, 1]}}\n"
        f"    verible_format: {{cmd: '{py} {tmp_path}/fake_verible.py {{args}} {{files}}', parse: verible}}\n")
    (tmp_path / "src" / "rtl").mkdir(parents=True)
    fake = ScriptedLLM()
    monkeypatch.setattr(runtime, "run_stage", fake)
    eng = Engine(Project.open(tmp_path))
    return tmp_path, eng, fake


def _run(eng, name):
    anyio.run(lambda: eng.run_step(eng.by_name[name]))


def _flags_answer(stage):
    return RulesResult(summary="ok", verible_flags=["--ruleset=none", "--rules=+no-tabs"], format_flags=["--indentation_spaces=2"],
                       mapping=["FMT-01: no-tabs"], not_checkable=["NAM-05"])


def test_rule_ids_and_their_diff():
    old = parse_rules("- **NAM-01** names use prefixes\n- **FMT-01** no tabs\nplain text\n")
    new = parse_rules("- **NAM-01** names use prefixes and suffixes\n- **FMT-02** two spaces\n")
    assert set(old) == {"NAM-01", "FMT-01"}
    assert diff_rules(old, new) == ["changed NAM-01: - **NAM-01** names use prefixes and suffixes", "added FMT-02: - **FMT-02** two spaces", "removed FMT-01"]


def test_rules_step_writes_the_flags_and_updates_by_diff(rtl_proj):
    root, eng, fake = rtl_proj
    fake.answers["rules_style"] = _flags_answer
    _run(eng, "rules")
    sd = root / "src" / "rtl" / "style_lint"
    assert (sd / "verible.flags").read_text().splitlines()[1:] == ["--ruleset=none", "--rules=+no-tabs"]
    assert "--indentation_spaces=2" in (sd / "format.flags").read_text() and (sd / "rules_resolved.md").is_file()
    first = fake.calls[0]
    assert "verible-flags" in first.prompt and "SKILL.md" in first.prompt  # the skills are offered, not pasted
    assert "- line-length* — " in first.prompt and "[length=100]" in first.prompt  # the installed Verible's rules, from `--help_rules=all`
    assert {t.name for t in first.sdk_tools} == {"verible_help", "verible_check_flags", "verible_lint"}
    assert first.output_model is RulesResult and root / "src" / "rtl" in first.deny_dirs  # (the rules are read, never the RTL)
    # nothing changed: no LLM
    n = len(fake.calls)
    _run(eng, "rules")
    assert len(fake.calls) == n
    # your rules file: the task names exactly the rule that is new
    (root / "config").mkdir(exist_ok=True)
    (root / "config" / "rtl_rules.md").write_text("- **USR-01** lines are at most 100 characters\n")
    _run(eng, "rules")
    assert len(fake.calls) == n + 1 and "added USR-01" in fake.calls[-1].prompt and "--rules=+no-tabs" in fake.calls[-1].prompt


def test_rules_that_contradict_stop_the_flow_before_any_rtl(rtl_proj):
    root, eng, fake = rtl_proj
    fake.answers["rules_style"] = lambda s: RulesResult(
        verible_flags=["--rules=+no-tabs"], findings=[RuleFinding(severity="error", ids=["NAM-07", "USR-02"], problem="max 20 vs names of 30",
                                                                  suggestion="raise USR-02 to 32")])
    with pytest.raises(StepFailed, match="NAM-07, USR-02"):
        _run(eng, "rules")
    assert not (root / "src" / "rtl" / "style_lint" / "verible.flags").exists() and (root / "src" / "rtl" / "style_lint" / "rules_check.json").is_file()


def test_bad_flags_are_sent_back_to_the_llm(rtl_proj):
    root, eng, fake = rtl_proj
    answers = iter([RulesResult(verible_flags=["--lint_fatal"]), RulesResult(verible_flags=["--rules=+no-tabs"])])
    fake.answers["rules_style"] = lambda s: next(answers)
    fake.answers["rules_style_fix"] = lambda s: next(answers)
    _run(eng, "rules")
    assert fake.names() == ["rules_style", "rules_style_fix"] and "not allowed" in fake.calls[1].prompt


def test_format_changes_whitespace_only_and_keeps_a_backup(rtl_proj):
    from q3tui.core.project import sha256_file
    from q3tui.pipeline.state import StepRecord

    root, eng, fake = rtl_proj
    (root / "src" / "rtl" / "a.sv").write_text("module  a;\n  wire   x;\nendmodule\n")
    (root / "src" / "rtl" / "b.sv").write_text("module b;\n  wire x; // CORRUPT\nendmodule\n")
    (root / "src" / "rtl" / "c.sv").write_text("module c; SYNTAX\nendmodule\n")
    eng.state.steps["rtl"] = StepRecord(status="done", outputs={f"src/rtl/{n}.sv": sha256_file(root / "src" / "rtl" / f"{n}.sv") for n in "abc"})
    _run(eng, "format")
    assert (root / "src" / "rtl" / "a.sv").read_text() == "module a;\n wire x;\nendmodule\n"
    assert "wire x" in (root / "src" / "rtl" / "b.sv").read_text()  # the formatter changed code (wire y): refused
    rep = eng.by_name["format"].read_json(root / "src" / "rtl" / "style_lint" / "format.json")
    assert rep["formatted"] == ["a.sv"] and rep["rejected_changed_code"] == ["b.sv"] and list(rep["syntax_errors"]) == ["c.sv"]
    assert list((root / ".q3tui" / "bkp").glob("*/src/rtl/a.sv"))
    # the format is not a hand edit: the rtl step's record follows it
    assert eng.state.steps["rtl"].outputs["src/rtl/a.sv"] == sha256_file(root / "src" / "rtl" / "a.sv")
    assert eng.evaluate(eng.by_name["rtl"]).edited == []


def test_format_asks_before_touching_a_file_you_edited(rtl_proj):
    from q3tui.core.project import sha256_file
    from q3tui.pipeline.state import StepRecord

    root, eng, fake = rtl_proj
    f = root / "src" / "rtl" / "a.sv"
    f.write_text("module  a;\nendmodule\n")
    eng.state.steps["rtl"] = StepRecord(status="done", outputs={"src/rtl/a.sv": sha256_file(f)})
    f.write_text("module   a; // mine\nendmodule\n")  # your edit
    _run(eng, "format")
    q = eng.by_name["format"].questions_list(eng)
    assert len(q) == 1 and q[0].blocking and "edited by hand" in q[0].question and f.read_text().startswith("module   a;")
    assert eng.evaluate(eng.by_name["rtl"]).edited == ["src/rtl/a.sv"]  # still yours
    eng.by_name["format"].answer(eng, q[0].id, "yes — format it (a backup is kept)")
    _run(eng, "format")
    assert f.read_text() == "module a; // mine\nendmodule\n" and list((root / ".q3tui" / "bkp").glob("*/src/rtl/a.sv"))
    assert eng.evaluate(eng.by_name["rtl"]).edited == ["src/rtl/a.sv"]  # your edit is still flagged as yours


def _style_setup(root, eng, body="module m;\n  parameter BAD_NAME = 1;\nendmodule\n"):
    (root / "src" / "rtl" / "m.sv").write_text(body)
    sd = root / "src" / "rtl" / "style_lint"
    sd.mkdir()
    (sd / "verible.flags").write_text("--rules=+no-tabs\n")


def test_style_lint_clean_needs_no_llm(rtl_proj):
    root, eng, fake = rtl_proj
    _style_setup(root, eng, "module m;\nendmodule\n")
    _run(eng, "style_lint")
    assert fake.calls == []
    assert eng.by_name["style_lint"].read_json(root / "src" / "rtl" / "style_lint" / "style_report.json")["status"] == "clean"


def test_style_lint_hands_fixes_back_to_their_owners(rtl_proj):
    root, eng, fake = rtl_proj
    _style_setup(root, eng)
    fake.answers["style_triage"] = lambda s: StyleVerdict(
        summary="one violation", fixes=[Fix(module="m", request="line 2: rename BAD_NAME to PARA_W (NAM-02)")], flag_problems=[])
    _run(eng, "style_lint")
    st = fake.calls[0]
    assert "parameter-name-style" in st.prompt and "m:2" in st.prompt and "lint-triage" in st.prompt
    assert eng.state.feedback["rtl"] == ["[rtl:m] From style_lint (Verible): line 2: rename BAD_NAME to PARA_W (NAM-02)"]
    assert eng.state.step_meta["style_lint"]["loop"] is True  # the engine runs the owners, then this step again


def test_a_flag_problem_goes_to_the_rules_step_and_waivers_are_remembered(rtl_proj):
    root, eng, fake = rtl_proj
    _style_setup(root, eng, "module m;\n  parameter BAD_NAME = 1;\n  wire w;   \nendmodule\n")
    fake.answers["style_triage"] = lambda s: StyleVerdict(
        flag_problems=[], fixes=[Fix(module="m", request="remove trailing spaces on line 3")],
        waived=[Waiver(module="m", rule="parameter-name-style", why="NAM-02 allows names fixed by the protocol")])
    _run(eng, "style_lint")
    assert eng.state.step_meta["style_lint"]["waived"]
    fake.answers["style_triage"] = lambda s: StyleVerdict(flag_problems=[], fixes=[])
    (root / "src" / "rtl" / "m.sv").write_text("module m;\n  parameter BAD_NAME = 1;\nendmodule\n")  # fixed; the accepted finding is not judged again
    n = len(fake.calls)
    _run(eng, "style_lint")
    assert len(fake.calls) == n


def test_style_lint_stops_after_max_rounds(rtl_proj):
    root, eng, fake = rtl_proj
    _style_setup(root, eng)
    eng.by_name["style_lint"].options["max_rounds"] = 1
    fake.answers["style_triage"] = lambda s: StyleVerdict(fixes=[Fix(module="m", request="rename it")])
    _run(eng, "style_lint")  # round 1: handed back
    eng.state.step_meta["style_lint"]["rounds"] = 1  # (same run: the engine loop counts the rounds)
    with pytest.raises(StepFailed, match="after 1 fix round"):
        _run(eng, "style_lint")


def test_the_verible_tools_run_through_the_roles(rtl_proj):
    from q3tui.flows.rtl.lib.tools import verible_tools

    root, eng, fake = rtl_proj
    _style_setup(root, eng)
    ts = eng.by_name["style_lint"].tools(eng)
    tools = {t.name: t for t in verible_tools(ts, root / ".q3tui" / "w", root, lambda: sorted((root / "src" / "rtl").glob("*.sv")), root / "src" / "rtl" / "style_lint" / "verible.flags")}

    def call(name, **args):
        out = anyio.run(lambda: tools[name].handler(args))
        return out["content"][0]["text"]

    assert "parameter-name-style" in call("verible_help", rule="all") and "line-length" in call("verible_help", rule="all")
    assert call("verible_check_flags", flags=["--rules=+no-tabs"]).startswith("accepted")
    assert "refused its flags" in call("verible_check_flags", flags=["--rules=+bogus-rule"])
    assert "not allowed" in call("verible_check_flags", flags=["--lint_fatal"])
    lint = call("verible_lint")
    assert "m:2 [parameter-name-style]" in lint and "flags used: --rules=+no-tabs" in lint


def test_the_fix_loop_runs_by_itself_rtl_format_style_rtl_format_style(rtl_proj):
    """generate → format → style check → fix (back to rtl, that module) → format → check → clean, in one `run`."""
    from q3tui import flows
    from q3tui.steps.common import split_feedback

    root, eng0, fake = rtl_proj
    flow = flows.load_flow("rtl", root)
    steps = {s.name: s for s in flows.build_steps(flow)}
    ran: list[str] = []
    real_rtl = steps["rtl"]

    class StubRtl(type(real_rtl)):  # the real step's options / layout / rule stack, a trivial generator
        deps = ()

        def inputs(self, engine):
            return [engine.project.root / "spec.txt"]

        def outputs(self, engine):
            return sorted((engine.project.root / "src" / "rtl").glob("*.sv"))

        def missing_input(self, engine):
            return None

        async def run(self, ctx):
            ran.append("rtl")
            f = ctx.project.root / "src" / "rtl" / "m.sv"
            by_unit, _ = split_feedback(ctx.feedback, "rtl")
            if not f.is_file():
                f.write_text("module  m;\n  parameter  BAD_NAME = 1;   \nendmodule\n")
            elif "m" in by_unit:
                f.write_text(f.read_text().replace("BAD_NAME", "PARA_W"))

    stub = StubRtl()
    stub.name, stub.kind, stub.options = "rtl", real_rtl.kind, dict(real_rtl.options)
    for n in ("format", "style_lint"):
        orig = steps[n].run

        async def counted(ctx, n=n, orig=orig):
            ran.append(n)
            await orig(ctx)

        steps[n].run = counted
    (root / "spec.txt").write_text("x")
    (root / "src" / "rtl" / "style_lint").mkdir(parents=True)
    (root / "src" / "rtl" / "style_lint" / "verible.flags").write_text("--rules=+no-tabs\n")
    eng = Engine(Project.open(root), steps=[stub, steps["format"], steps["style_lint"]], flow=flow)
    fake.answers["style_triage"] = lambda s: StyleVerdict(summary="rename", fixes=[Fix(module="m", request="line 2: rename BAD_NAME to PARA_W")])
    assert anyio.run(lambda: eng.run(yes=True)) == "complete"
    assert ran == ["rtl", "format", "style_lint", "rtl", "format", "style_lint"]
    assert (root / "src" / "rtl" / "m.sv").read_text() == "module m;\n parameter PARA_W = 1;\nendmodule\n"  # fixed, then formatted
    assert fake.names() == ["style_triage"]  # the second check is clean: no LLM
    rep = eng.by_name["style_lint"].read_json(root / "src" / "rtl" / "style_lint" / "style_report.json")
    assert rep["status"] == "clean" and eng.state.feedback.get("rtl", []) == []
    assert [v.status for v in eng.status()] == ["done", "done", "done"]


# -- smoke ---------------------------------------------------------------------------------------------------------

from q3tui.flows.rtl.lib import smoke as smokelib

FAKE_SIM = '''
import sys
for f in [l.strip() for l in open(sys.argv[1]) if l.strip()]:
    if "COMPILE_ERR" in open(f).read():
        print(f"{f}:2: error: port i_x not found")
        sys.exit(1)
'''
FAKE_RUN = '''
import sys
files = [l.strip() for l in open(sys.argv[1] + "/filelist_smoke_abs.f") if l.strip()]
text = "".join(open(f).read() for f in files)
bad = "RTL_BUG" in text or "TB_BUG" in text
print("SMOKE_RESULT S1 PASS")
print("SMOKE_RESULT S2 " + ("FAIL expected 5 saw 4" if bad else "PASS"))
print(f"SMOKE_SUMMARY total=2 failed={1 if bad else 0}")
'''
SPEC_JSON = {"metadata": {"top_module": "m"}, "requirements": [{"req_id": "REQ-001", "text": "o_y follows i_a after one cycle"}]}
CONFIG_JSON = {"parameters": {"PR_W": {"value": 8}}}


@pytest.fixture
def smoke_proj(tmp_path, monkeypatch):
    import json

    py = sys.executable
    (tmp_path / "fake_sim.py").write_text(FAKE_SIM)
    (tmp_path / "fake_run.py").write_text(FAKE_RUN)
    (tmp_path / "q3tui.yaml").write_text(
        "pipeline: {flow: rtl}\ntools:\n  roles:\n"
        f"    sim: {{cmd: '{py} {tmp_path}/fake_sim.py {{filelist}}', parse: iverilog}}\n"
        f"    run: {{cmd: '{py} {tmp_path}/fake_run.py {{workdir}}', parse: generic}}\n")
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec" / "spec.md").write_text("# m\nThe module m copies i_a to o_y one cycle later.\n")
    (tmp_path / "schemas").mkdir()
    (tmp_path / "schemas" / "structured_spec.json").write_text(json.dumps(SPEC_JSON))
    (tmp_path / "schemas" / "final_config.json").write_text(json.dumps(CONFIG_JSON))
    (tmp_path / "src" / "rtl").mkdir(parents=True)
    (tmp_path / "src" / "rtl" / "m.sv").write_text("module m(input i_clk, input [7:0] i_a, output [7:0] o_y);\nendmodule\n")
    (tmp_path / "src" / "rtl" / "style_lint").mkdir(parents=True)
    (tmp_path / "src" / "rtl" / "style_lint" / "verible.flags").write_text("--rules=+no-tabs\n")
    fake = ScriptedLLM()

    def writer(stage):
        stage.write_files[0].write_text("module tb_smoke; // " + ("TB_BUG" if fake.tb_bug else "ok") + "\nendmodule\n")
        return SmokePlan(behaviours=[Behaviour(id="S1", description="reset", req_ids=["REQ-001"]), Behaviour(id="S2", description="copy", req_ids=["REQ-001"])])

    fake.tb_bug = False
    fake.answers["smoke_tb"] = writer
    monkeypatch.setattr(runtime, "run_stage", fake)
    return tmp_path, Engine(Project.open(tmp_path)), fake


def test_smoke_parse_run():
    ok = "SMOKE_RESULT S1 PASS\nSMOKE_RESULT S2 PASS\nSMOKE_SUMMARY total=2 failed=0\n"
    assert smokelib.parse_run(ok, 0, False).status == "pass"
    bad = smokelib.parse_run("SMOKE_RESULT S1 PASS\nSMOKE_RESULT S2 FAIL expected 5, saw 4\n", 0, False)
    assert bad.status == "fail" and bad.results["S2"]["detail"] == "expected 5, saw 4" and "S2" in bad.reason
    assert smokelib.parse_run("hello", 0, False).status == "no_result"  # no marker: never a pass
    assert smokelib.parse_run("SMOKE_RESULT S1 PASS\n", 0, True).status == "timeout"
    assert smokelib.parse_run("SMOKE_RESULT S1 PASS\nSMOKE_SUMMARY total=3 failed=0\n", 0, False).status == "fail"  # the summary must agree


def test_smoke_writer_never_sees_the_rtl_and_the_result_is_code(smoke_proj):
    root, eng, fake = smoke_proj
    _run(eng, "smoke")
    w = fake.calls[0]
    assert w.name == "smoke_tb" and {t.name for t in w.sdk_tools} == {"smoke_compile", "smoke_run"}
    assert "o_y follows i_a" in w.prompt and "PR_W = 8" in w.prompt and "smoke-testbench" in w.prompt
    assert "input [7:0] i_a" not in w.prompt  # the RTL's text is nowhere in the task
    assert root / "src" / "rtl" / "m.sv" in w.deny_dirs and root / "src" / "rtl" / "style_lint" in w.deny_dirs
    assert root / "src" / "rtl" / "smoke" not in w.deny_dirs and w.write_files == [root / "src" / "rtl" / "smoke" / "tb_smoke.sv"]
    tb = w.write_files[0]
    assert not any(d == tb or d in tb.parents for d in w.deny_dirs)  # (no denied folder holds the file it must write)
    rep = eng.by_name["smoke"].read_json(root / "src" / "rtl" / "smoke" / "smoke_report.json")
    assert rep["status"] == "pass" and rep["run"]["results"]["S2"]["status"] == "PASS"
    assert "| S2 | copy | REQ-001 |" in (root / "src" / "rtl" / "smoke" / "smoke_plan.md").read_text()
    assert "m.sv" in (root / "src" / "rtl" / "smoke" / "filelist.f").read_text()
    # RTL changed, spec the same: it compiles and runs again, the testbench is not rewritten (update, not re-run)
    (root / "src" / "rtl" / "m.sv").write_text("module m(input i_clk, input [7:0] i_a, output [7:0] o_y); // v2\nendmodule\n")
    assert eng.evaluate(eng.by_name["smoke"]).status == "stale"
    _run(eng, "smoke")
    assert fake.names() == ["smoke_tb"]
    # a requirement changed: the writer is asked to update, told what changed
    (root / "schemas" / "structured_spec.json").write_text(__import__("json").dumps(
        {**SPEC_JSON, "requirements": [{"req_id": "REQ-001", "text": "o_y follows i_a after two cycles"}]}))
    _run(eng, "smoke")
    assert fake.names() == ["smoke_tb", "smoke_tb"] and "REQ-001 changed: o_y follows i_a after two cycles" in fake.calls[1].prompt and "Current testbench" in fake.calls[1].prompt


def test_an_rtl_bug_goes_back_to_rtl_and_the_loop_is_set(smoke_proj):
    root, eng, fake = smoke_proj
    (root / "src" / "rtl" / "m.sv").write_text("module m; // RTL_BUG\nendmodule\n")
    fake.answers["smoke_triage"] = lambda s: SmokeTriage(verdicts=[Verdict(id="S2", verdict="rtl_bug", module="m", summary="o_y is off by one",
                                                                          request="register i_a once, o_y = that register")])
    _run(eng, "smoke")
    t = fake.calls[-1]
    assert t.name == "smoke_triage" and "rtl/m.sv" in t.prompt and root / "src" / "rtl" / "m.sv" not in t.deny_dirs  # the judge may read the RTL
    assert "S2: FAIL" in t.prompt and "expected 5 saw 4" in t.prompt and "smoke-triage" in t.prompt
    assert eng.state.feedback["rtl"] == ["[rtl:m] From the smoke test (S2): o_y is off by one register i_a once, o_y = that register"]
    assert eng.state.step_meta["smoke"]["loop"] is True
    assert eng.by_name["smoke"].missing_input(eng).startswith("waiting for the fixes handed to rtl")  # the chain runs the fix first


def test_two_failures_with_one_cause_make_one_request_per_module(smoke_proj):
    root, eng, fake = smoke_proj
    (root / "src" / "rtl" / "m.sv").write_text("module m; // RTL_BUG\nendmodule\n")
    fake.answers["smoke_triage"] = lambda s: SmokeTriage(verdicts=[
        Verdict(id="S2", verdict="rtl_bug", module="m", summary="adds 2", request="add 1"),
        Verdict(id="S4", verdict="rtl_bug", module="m", summary="never wraps", request="add 1")])
    _run(eng, "smoke")
    assert eng.state.feedback["rtl"] == ["[rtl:m] From the smoke test (S2, S4): adds 2 add 1 | never wraps add 1"]


def test_a_testbench_bug_comes_back_to_the_writer(smoke_proj):
    root, eng, fake = smoke_proj
    fake.tb_bug = True
    fake.answers["smoke_triage"] = lambda s: SmokeTriage(verdicts=[Verdict(id="S2", verdict="tb_bug", summary="expected value computed wrongly",
                                                                          request="expect 4 after one cycle")])
    _run(eng, "smoke")
    assert eng.state.feedback["smoke"] and "expect 4 after one cycle" in eng.state.feedback["smoke"][0]
    fake.tb_bug = False
    _run(eng, "smoke")  # the change request reaches the writer; the result is now a pass
    assert fake.names() == ["smoke_tb", "smoke_triage", "smoke_tb"] and "expect 4 after one cycle" in fake.calls[2].prompt
    assert eng.by_name["smoke"].read_json(root / "src" / "rtl" / "smoke" / "smoke_report.json")["status"] == "pass"


def test_a_spec_issue_is_a_blocking_question_and_a_compile_error_is_triaged(smoke_proj):
    root, eng, fake = smoke_proj
    (root / "src" / "rtl" / "m.sv").write_text("module m; // COMPILE_ERR\nendmodule\n")
    fake.answers["smoke_triage"] = lambda s: SmokeTriage(verdicts=[Verdict(id="compile", verdict="spec_issue", summary="the spec never names the enable port")])
    _run(eng, "smoke")
    assert "port i_x not found" in fake.calls[-1].prompt
    q = eng.by_name["smoke"].questions_list(eng)
    assert len(q) == 1 and q[0].blocking and q[0].kind == "spec_gap" and "enable port" in q[0].question
    assert eng.state.step_meta["smoke"]["loop"] is False  # nobody can fix it but you


def test_smoke_fails_after_max_rounds_and_tools_must_exist(smoke_proj):
    root, eng, fake = smoke_proj
    (root / "src" / "rtl" / "m.sv").write_text("module m; // RTL_BUG\nendmodule\n")
    fake.answers["smoke_triage"] = lambda s: SmokeTriage(verdicts=[Verdict(id="S2", verdict="rtl_bug", module="m", summary="off by one", request="fix the delay")])
    eng.by_name["smoke"].options["max_rounds"] = 1
    _run(eng, "smoke")
    eng.state.step_meta["smoke"]["rounds"] = 1
    with pytest.raises(StepFailed, match="still failing after 1 fix round"):
        _run(eng, "smoke")
    (root / "q3tui.yaml").write_text("pipeline: {flow: rtl}\ntools:\n  roles:\n    sim: {cmd: 'no_such_simulator {filelist}'}\n    run: {cmd: 'x'}\n")
    eng2 = Engine(Project.open(root))
    eng2.state.feedback.clear()  # (the fix handed to rtl above is not what this part is about)
    assert "no_such_simulator" in eng2.by_name["smoke"].missing_input(eng2)  # a check without its tool waits; it is not passed


def test_the_whole_chain_runs_by_itself_until_the_smoke_test_passes(smoke_proj):
    """rtl → format → style_lint → smoke fails (RTL bug) → triage → fix to rtl → format → style_lint → smoke passes: one `run`."""
    from q3tui import flows
    from q3tui.steps.common import split_feedback

    root, _, fake = smoke_proj
    py = sys.executable
    (root / "fake_verible.py").write_text(FAKE_VERIBLE)
    cfg = (root / "q3tui.yaml").read_text()
    (root / "q3tui.yaml").write_text(cfg + f"    verible_lint: {{cmd: '{py} {root}/fake_verible.py {{args}} {{files}}', parse: verible, ok_returncodes: [0, 1]}}\n"
                                     f"    verible_format: {{cmd: '{py} {root}/fake_verible.py {{args}} {{files}}', parse: verible}}\n")
    flow = flows.load_flow("rtl", root)
    steps = {s.name: s for s in flows.build_steps(flow)}
    ran: list[str] = []
    real_rtl = steps["rtl"]

    class StubRtl(type(real_rtl)):
        deps = ()

        def inputs(self, engine):
            return [engine.project.root / "spec.txt"]

        def outputs(self, engine):
            return sorted((engine.project.root / "src" / "rtl").glob("*.sv"))

        def missing_input(self, engine):
            return None

        async def run(self, ctx):
            ran.append("rtl")
            f = ctx.project.root / "src" / "rtl" / "m.sv"
            by_unit, _ = split_feedback(ctx.feedback, "rtl")
            if "m" in by_unit:
                f.write_text(f.read_text().replace("RTL_BUG", "fixed"))
            elif not f.is_file() or "RTL_BUG" not in f.read_text():
                f.write_text("module m; // RTL_BUG\nendmodule\n")

    stub = StubRtl()
    stub.name, stub.kind, stub.options = "rtl", real_rtl.kind, dict(real_rtl.options)
    for n in ("format", "style_lint", "smoke"):
        orig = steps[n].run

        async def counted(ctx, n=n, orig=orig):
            ran.append(n)
            await orig(ctx)

        steps[n].run = counted
    (root / "spec.txt").write_text("x")
    (root / "src" / "rtl" / "m.sv").unlink()
    eng = Engine(Project.open(root), steps=[stub, steps["format"], steps["style_lint"], steps["smoke"]], flow=flow)
    fake.answers["smoke_triage"] = lambda s: SmokeTriage(verdicts=[Verdict(id="S2", verdict="rtl_bug", module="m", summary="off by one", request="fix the delay")])
    assert anyio.run(lambda: eng.run(yes=True)) == "complete"
    assert ran == ["rtl", "format", "style_lint", "smoke", "rtl", "format", "style_lint", "smoke"]
    assert fake.names() == ["smoke_tb", "smoke_triage"]  # the testbench was written once, the triage asked once, style never needed the LLM
    rep = eng.by_name["smoke"].read_json(root / "src" / "rtl" / "smoke" / "smoke_report.json")
    assert rep["status"] == "pass" and "RTL_BUG" not in (root / "src" / "rtl" / "m.sv").read_text()
    assert [v.status for v in eng.status()] == ["done"] * 4 and eng.state.feedback.get("rtl", []) == []


def test_the_addon_actions_and_settings(rtl_proj):
    from q3tui.core.ops import Ops

    root, eng, fake = rtl_proj
    assert {"style_lint", "smoke"} <= {s.path.split(".")[2] for s in eng.flow.settings if s.path.startswith("pipeline.step_options.")}
    ops = Ops(eng)
    assert "created" in ops.run_action("rtl_rules_init")
    assert "My RTL rules" in (root / "config" / "rtl_rules.md").read_text()
    with pytest.raises(Exception, match="exists already"):
        ops.run_action("rtl_rules_init")
    # your rules reach the real rtl step: it lists the file as an input and reads it first
    rtl = eng.by_name["rtl"]
    (root / "config" / "rtl_rules.md").write_text("- **USR-01** mine\n")
    assert root / "config" / "rtl_rules.md" in rtl.inputs(eng) and rtl.rule_files() == ["config/rtl_rules.md", "rtl_rule_default.md"]
    (root / "src" / "rtl" / "a.sv").write_text("module  a;\nendmodule\n")
    (root / "src" / "rtl" / "style_lint").mkdir(parents=True)
    (root / "src" / "rtl" / "style_lint" / "verible.flags").write_text("--rules=+no-tabs\n")
    assert "--rules=+no-tabs" in ops.run_action("style_show") and "rules (first wins): config/rtl_rules.md" in ops.run_action("style_show")
    assert "1 file(s) formatted" in ops.run_action("format_now", files="a")
    assert (root / "src" / "rtl" / "a.sv").read_text() == "module a;\nendmodule\n"


# -- the real `rtl` step in this flow (the VLSIT step with the fake stages of tests.test_rtl_gen.py) -----------

from tests.test_rtl_gen import calls, config_json, llm, spec_json, tools_dir, tools_yaml  # noqa: E402,F401  (fixtures)


def test_the_rtl_step_writes_to_rtl_and_follows_your_rules(tmp_path, tools_dir, llm):
    from q3tui.flows.spec.lib import artifacts

    (tmp_path / "q3tui.yaml").write_text("pipeline: {flow: rtl, step_options: {rtl: {rules: [rtl_rule.md]}}}\n" + tools_yaml(tools_dir))
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec" / "spec.md").write_text("# demo spec\n")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "rtl_rules.md").write_text("- **USR-01** Every module has a header comment with its owner.\n")
    project = Project.open(tmp_path)
    artifacts.write(project, "structured_spec.json", spec_json(), check=False)
    artifacts.write(project, "final_config.json", config_json(), check=False)
    eng = Engine(project)
    anyio.run(lambda: eng.run_step(eng.by_name["rtl"]))
    assert sorted(p.name for p in (tmp_path / "src" / "rtl").glob("*.sv")) == ["leaf_a.sv", "leaf_b.sv", "top.sv"]
    assert (tmp_path / "src" / "rtl" / "filelist.f").read_text().split() == ["src/rtl/leaf_a.sv", "src/rtl/leaf_b.sv", "src/rtl/top.sv"]
    c = calls(llm, "rtl_top")[0]
    assert c.write_files == [tmp_path / "src" / "rtl" / "top.sv"]
    assert "USR-01" in c.system_prompt and c.system_prompt.index("rtl_rules.md") < c.system_prompt.index("rtl_rule.md")  # yours first: it wins
    # editing your rules updates the modules (the file is an input of the step)
    n = len(calls(llm, "rtl_"))
    (tmp_path / "config" / "rtl_rules.md").write_text("- **USR-01** Every module has a header comment with its owner and the date.\n")
    assert eng.evaluate(eng.by_name["rtl"]).status == "stale"
    anyio.run(lambda: eng.run_step(eng.by_name["rtl"]))
    assert eng.state.steps["rtl"].status == "done" and len(calls(llm, "rtl_")) >= n


def test_a_failure_that_keeps_coming_back_becomes_a_question_not_another_round(smoke_proj):
    root, eng, fake = smoke_proj
    (root / "src" / "rtl" / "m.sv").write_text("module m; // RTL_BUG\nendmodule\n")
    fake.answers["smoke_triage"] = lambda s: SmokeTriage(verdicts=[Verdict(id="S2", verdict="rtl_bug", module="m", summary="port names differ", request="rename the ports")])
    _run(eng, "smoke")  # round 1: handed back
    _run(eng, "smoke")  # round 2: handed back again (escalate_after_repeats = 2)
    assert len(eng.state.feedback["rtl"]) == 2 and "Already handed back" in fake.calls[-1].prompt and "rtl_bug|m|S2" in fake.calls[-1].prompt
    eng.state.feedback.pop("rtl")
    _run(eng, "smoke")  # the same verdict a third time: the user decides
    q = eng.by_name["smoke"].questions_list(eng)
    assert len(q) == 1 and q[0].blocking and "keeps failing" in q[0].question and "3 times" in q[0].question and "rtl" not in eng.state.feedback
