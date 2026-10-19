"""The RTL flow against a REAL Verible (skipped when it is not installed: put `verible-verilog-lint` / `-format` on PATH, or set
VERIBLE_BIN to the folder holding them). What a fake tool cannot tell: the log formats, exit codes and flag syntax (docs/spec/rtl-flow.md)."""

import os
import shutil
from pathlib import Path

import anyio
import pytest

from q3tui.core.project import Project
from q3tui.flows.rtl.lib import verible
from q3tui.flows.rtl.lib.tools import verible_tools
from q3tui.pipeline.engine import Engine
from q3tui.pipeline.state import StepRecord

BIN = os.environ.get("VERIBLE_BIN")
LINT = shutil.which("verible-verilog-lint", path=BIN or os.environ.get("PATH"))
FORMAT = shutil.which("verible-verilog-format", path=BIN or os.environ.get("PATH"))
pytestmark = pytest.mark.skipif(not (LINT and FORMAT), reason="Verible is not installed")

BAD = "module Bad_Mod (input logic clk, input logic rst_n, output logic [3:0] q);\n  parameter Width = 4;\n\tlogic [3:0] cnt;   \n" \
      "  always_ff @(posedge clk) if (!rst_n) cnt <= '0; else cnt <= cnt + 1;\n  assign q = cnt;\nendmodule\n"


@pytest.fixture
def proj(tmp_path):
    (tmp_path / "q3tui.yaml").write_text(f"pipeline: {{flow: rtl}}\ntools:\n  roles:\n"
                                         f"    verible_lint: {{cmd: '{LINT} {{args}} {{files}}', parse: verible, ok_returncodes: [0, 1]}}\n"
                                         f"    verible_format: {{cmd: '{FORMAT} {{args}} {{files}}', parse: verible}}\n")
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "Bad_Mod.sv").write_text(BAD)
    return tmp_path, Engine(Project.open(tmp_path))


def lint(eng, root, flags):
    ts = eng.by_name["style_lint"].tools(eng)
    return verible.run_lint(ts, root / ".q3tui" / "w", [root / "rtl" / "Bad_Mod.sv"], flags), ts


def test_lint_findings_and_exit_codes(proj):
    root, eng = proj
    res, _ = lint(eng, root, ["--ruleset=none", "--rules=+no-tabs,+no-trailing-spaces"])
    assert verible.problem(res, "verible_lint") is None  # exit code 1 with findings is a result
    found = verible.findings(res.diagnostics, root)
    assert [(f.module, f.line, f.rule) for f in found] == [("Bad_Mod", 3, "no-tabs"), ("Bad_Mod", 3, "no-trailing-spaces")]
    assert found[0].text.strip() == "logic [3:0] cnt;"
    clean, _ = lint(eng, root, ["--ruleset=none", "--rules=+module-filename"])
    assert verible.problem(clean, "verible_lint") is None and verible.findings(clean.diagnostics, root) == []


def test_a_bad_flag_is_the_tools_refusal_never_a_clean_result(proj):
    root, eng = proj
    for flags, why in ((["--rules=+bogus-rule"], "bogus-rule"), (["--ruleset=none", "--rules=+line-length=length:20"], "out of range"),
                       (["--ruleset=none", "--rules=+parameter-name-style=parameter_style:XX"], "available choices")):
        res, _ = lint(eng, root, flags)
        msg = verible.problem(res, "verible_lint")
        assert msg and "refused its flags" in msg and why in msg


def test_naming_rule_with_your_regex_needs_the_style_emptied(proj):
    root, eng = proj
    rx = "parameter_style_regex:PARA_[A-Z0-9_]+"
    res, _ = lint(eng, root, ["--ruleset=none", f"--rules=+parameter-name-style={rx}"])
    assert verible.findings(res.diagnostics, root) == []  # `Width` is CamelCase: the built-in style lets it pass
    res, _ = lint(eng, root, ["--ruleset=none", f"--rules=+parameter-name-style=parameter_style:;{rx}"])
    assert [f.rule for f in verible.findings(res.diagnostics, root)] == ["parameter-name-style"]


def test_the_tools_the_llm_gets(proj):
    root, eng = proj
    ts = eng.by_name["style_lint"].tools(eng)
    flags = root / "rtl" / "style_lint" / "verible.flags"
    flags.parent.mkdir()
    flags.write_text("--ruleset=none\n--rules=+no-tabs\n")
    tools = {t.name: t for t in verible_tools(ts, root / ".q3tui" / "w", root, lambda: sorted((root / "rtl").glob("*.sv")), flags)}

    def call(name, **args):
        return anyio.run(lambda: tools[name].handler(args))["content"][0]["text"]

    assert "line-length" in call("verible_help", rule="all") and "length" in call("verible_help", rule="line-length")
    assert call("verible_check_flags", flags=["--ruleset=none", "--rules=+no-tabs"]).startswith("accepted")
    assert "refused its flags" in call("verible_check_flags", flags=["--rules=+bogus-rule"])
    out = call("verible_lint")
    assert "Bad_Mod:3 [no-tabs]" in out and "flags used: --ruleset=none --rules=+no-tabs" in out


def test_format_changes_whitespace_only_and_a_syntax_error_is_seen(proj):
    root, eng = proj
    f = root / "rtl" / "Bad_Mod.sv"
    (root / "rtl" / "syn.sv").write_text("module x;\n  wire a\nendmodule\n")
    eng.state.steps["rtl"] = StepRecord(status="done", outputs={"rtl/Bad_Mod.sv": __import__("q3tui.core.project", fromlist=["x"]).sha256_file(f)})
    step = eng.by_name["format"]
    (root / "rtl" / "style_lint").mkdir()
    (root / "rtl" / "style_lint" / "format.flags").write_text("--indentation_spaces=4\n")
    changed, rejected, syntax = step.apply(eng, step.rtl_files(eng), [])
    assert changed == ["Bad_Mod.sv"] and rejected == [] and list(syntax) == ["syn.sv"]  # (the formatter exits 0 on the syntax error: still seen)
    text = f.read_text()
    assert "    input logic clk," in text and "\t" not in text and not any(ln != ln.rstrip() for ln in text.splitlines())
    assert verible.same_code(BAD, text) and (root / "rtl" / "syn.sv").read_text().startswith("module x;")
    assert list((root / ".q3tui" / "bkp").glob("*/rtl/Bad_Mod.sv"))
