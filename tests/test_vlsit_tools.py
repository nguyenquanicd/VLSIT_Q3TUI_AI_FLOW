"""q3tui/eda/tools.py: tools by role from a file (render, log parsers, run_role, loading, roles a flow needs)."""

import json
import sys

import pytest

from q3tui.eda import tools as T
from q3tui.core.config import Config, ToolRole
from q3tui.eda.base import ToolUnavailable
from q3tui.flows import load_flow, parse_flow


def cfg(**tools) -> Config:
    return Config.model_validate({"tools": tools})


def test_render_fills_placeholders_and_expands_lists():
    r = ToolRole(cmd="verilator --lint-only -f {filelist} --top-module {top} {extra} --x={top}")
    assert T.render(r, {"filelist": "a b.f", "top": "t", "extra": ["-Wno-A", "-Wno-B"]}) == [
        "verilator", "--lint-only", "-f", "a b.f", "--top-module", "t", "-Wno-A", "-Wno-B", "--x=t"]
    assert T.render(ToolRole(cmd="tool {a}{b}"), {"a": "x", "b": 1}) == ["tool", "x1"]
    with pytest.raises(ToolUnavailable, match=r"needs \{top\}"):
        T.render(r, {"filelist": "f", "extra": []})
    assert T.binary_of(r) == "verilator"


def test_log_parsers():
    v = T.parse_log("verilator", "%Warning-UNUSED: rtl/a.sv:3:5: Signal is not used: 'x'\n%Error: rtl/a.sv:9:1: syntax error\n%Warning-WIDTH: b.sv:1: w")
    assert [(d.severity, d.code, d.file, d.line) for d in v] == [("warning", "UNUSED", "rtl/a.sv", 3), ("error", "ERROR", "rtl/a.sv", 9),
                                                                   ("warning", "WIDTH", "b.sv", 1)]
    i = T.parse_log("iverilog", "tb/t.sv:12: error: bad thing\nx.sv:3: warning: meh\nERROR: boom\n")
    assert [(d.severity, d.file) for d in i] == [("error", "tb/t.sv"), ("warning", "x.sv"), ("error", None)]
    y = T.parse_log("yosys", "Warning: something\nERROR: fatal thing at x.sv:3\n")
    assert [d.severity for d in y] == ["warning", "error"]
    g = T.parse_log("generic", "all fine\nFATAL: no\nsome Warning here\n")
    assert [d.severity for d in g] == ["error", "warning"]
    c = T.parse_log({"error": [r"^BAD"], "warning": [r"^HMM"]}, "BAD x\nHMM y\nerror: ignored by the custom patterns\n")
    assert [d.severity for d in c] == ["error", "warning"]
    vcs = T.parse_log("vcs", "Error-[SE] Syntax error\n  \"rtl/foo.sv\", 12: token is 'endmodule'\n")
    assert vcs and vcs[0].severity == "error" and vcs[0].file == "rtl/foo.sv"
    with pytest.raises(ToolUnavailable, match="unknown log parser"):
        T.parse_log("nope", "")


def script(tmp_path, body: str) -> str:
    p = tmp_path / "tool.py"
    p.write_text(body)
    return f"{sys.executable} {p}"


def test_run_role_runs_the_command_and_parses_the_log(tmp_path):
    cmd = script(tmp_path, "import sys\nprint('%Warning-UNUSED: a.sv:1:1: unused')\nprint('arg', sys.argv[1:])\nsys.exit(0)\n")
    ts = T.load_tools(cfg(roles={"lint": {"cmd": cmd + " {top} {files}", "parse": "verilator"}}), tmp_path)
    res = T.run_role(ts, "lint", tmp_path / "work", {"top": "t", "files": ["a.sv", "b.sv"]}, log_name="l.log")
    assert res.ok and res.returncode == 0 and [d.code for d in res.diagnostics] == ["UNUSED"]
    assert res.command[-3:] == ["t", "a.sv", "b.sv"] and res.summary == "lint: rc=0, 0 error(s), 1 warning(s)"
    assert "unused" in (tmp_path / "work" / "l.log").read_text() and res.log_path == str(tmp_path / "work" / "l.log")
    assert T.available(ts, "lint") and not T.available(ts, "missing")
    bad = script(tmp_path, "print('%Error: a.sv:2:1: boom')\n")  # rc 0 but an error diagnostic: not ok
    ts2 = T.load_tools(cfg(roles={"lint": {"cmd": bad, "parse": "verilator"}}), tmp_path)
    assert not T.run_role(ts2, "lint", tmp_path, {}).ok
    failing = script(tmp_path, "import sys\nsys.exit(3)\n")
    ts3 = T.load_tools(cfg(roles={"x": {"cmd": failing, "ok_returncodes": [0, 3]}, "y": {"cmd": failing}}), tmp_path)
    assert T.run_role(ts3, "x", tmp_path, {}).ok and not T.run_role(ts3, "y", tmp_path, {}).ok


def test_run_role_missing_binary_and_unknown_role(tmp_path):
    ts = T.load_tools(cfg(roles={"a": {"cmd": "definitely-not-a-tool-xyz --v"}}), tmp_path)
    res = T.run_role(ts, "a", tmp_path, {})
    assert not res.ok and res.returncode == 127 and not T.available(ts, "a")
    with pytest.raises(ToolUnavailable, match="no tool for role 'lint'"):
        ts.role("lint")


def test_supports_marks_capabilities(tmp_path):
    ts = T.load_tools(cfg(roles={"sim": {"cmd": "x", "supports": ["sva"]}, "sim2": {"cmd": "y"}}), tmp_path)
    assert T.supports(ts, "sim", "sva") and not T.supports(ts, "sim2", "sva") and not T.supports(ts, "nope", "sva")


def test_tools_file_and_inline_overlay(tmp_path, monkeypatch):
    monkeypatch.setenv("Q3TUI_HOME", str(tmp_path / "home"))
    (tmp_path / "tools.json").write_text(json.dumps({
        "setup_script": "env.sh", "modules": ["m1"], "timeout_s": 42,
        "roles": {"lint": {"cmd": "verilator {top}", "parse": "verilator"}, "synth": {"cmd": "yosys -c {script}", "parse": "yosys"}}}))
    c = cfg(roles={"synth": {"cmd": "yosys-custom {script}", "parse": "yosys"}})
    ts = T.load_tools(c, tmp_path)
    assert set(ts.roles) == {"lint", "synth"} and ts.roles["synth"].cmd.startswith("yosys-custom")  # inline wins
    assert ts.env.setup_script == "env.sh" and ts.env.modules == ("m1",) and ts.timeout_s == 42 and ts.source.endswith("tools.json")
    # tools.yaml and an explicit tools.file
    (tmp_path / "tools.json").unlink()
    (tmp_path / "t2.yaml").write_text("roles:\n  sim: {cmd: 'iverilog -f {filelist}', parse: iverilog}\n")
    assert set(T.load_tools(cfg(file="t2.yaml"), tmp_path).roles) == {"sim"}
    with pytest.raises(ToolUnavailable, match="not found"):
        T.load_tools(cfg(file="nope.json"), tmp_path)
    assert "built-in synopsys preset" in T.load_tools(cfg(), tmp_path).source  # nothing configured: the preset (test below)
    (tmp_path / "tools.yaml").write_text("roles: {a: {cmd: x {y}}}\n")  # unquoted braces: a YAML error, named
    with pytest.raises(ToolUnavailable, match="tools.yaml: not readable"):
        T.load_tools(cfg(), tmp_path)
    (tmp_path / "tools.yaml").write_text("- not a mapping\n")
    with pytest.raises(ToolUnavailable, match="mapping"):
        T.load_tools(cfg(), tmp_path)


def test_describe_for_llm_lists_roles(tmp_path):
    ts = T.load_tools(cfg(roles={"lint": {"cmd": "verilator {top}", "description": "RTL lint", "supports": ["x"]}}), tmp_path)
    text = T.describe_for_llm(ts, ["lint", "synth"])
    assert "- lint (supports: x): `verilator {top}` — RTL lint" in text and "- synth: not configured" in text


def test_roles_needed_by_a_flow_and_its_steps():
    flow = load_flow("vlsit")
    assert T.roles_needed(flow) == ["lint", "synth", "sim", "run"] or set(T.roles_needed(flow)) >= {"lint", "synth", "sim", "run"}
    f = parse_flow({"name": "x", "tools": ["pdf"], "steps": [{"id": "rtl", "kind": "vlsit_rtl", "options": {"tools": ["extra"]}}, {"id": "tb", "kind": "vlsit_tb"}]})
    assert T.roles_needed(f) == ["pdf", "lint", "synth", "extra", "sim"]
    assert T.roles_needed(load_flow("example")) == []


def test_design_compiler_parser_and_script():
    from pathlib import Path

    from q3tui.steps.vlsit import synth

    diags = T.parse_log("dc", "Error: /p/rtl/a.sv:12: syntax error near 'x'  (VER-294).\nWarning: Design has unmapped cells. (OPT-1006)\nInformation: x\n")
    assert [(d.severity, d.code, d.file, d.line) for d in diags] == [("error", "VER-294", "/p/rtl/a.sv", 12), ("warning", "OPT-1006", None, None)]
    log = "Version: T-2022.03-SP5\nNumber of cells:                          120\nTotal cell area:                  2345.5\n"
    assert synth.parse_stat(log, "top") == {"cell_count": 120, "area_um2": 2345.5, "tool": "Design Compiler T-2022.03-SP5"}
    assert synth.backend_for("dc") == "dc" and synth.backend_for("yosys") == "yosys" and synth.backend_for("dc", {"synth": {"backend": "yosys"}}) == "yosys"
    assert synth.script_suffix("dc") == ".tcl"
    text = synth.script_for("dc", [Path("/a.sv"), Path("/b.sv")], "top", "/lib/tt.db", {"synth": {"clock": {"port": "i_clk", "period_ns": 2}}})
    assert "analyze -format sverilog -work WORK { /a.sv /b.sv }" in text and "elaborate top" in text and "target_library [list /lib/tt.db]" in text
    assert "create_clock -name clk -period 2 [get_ports i_clk]" in text and "compile_ultra" in text and text.rstrip().endswith("exit")
    assert synth.corners({"synth": {"corners": [{"name": "tt", "library": "/x.db"}]}})[0]["liberty"] == "/x.db"
    assert synth.attribute_errors("Error: /p/leaf_a.sv:3: bad (VER-1)", ["leaf_a"]) == {"leaf_a": ["Error: /p/leaf_a.sv:3: bad (VER-1)"]}


def test_a_role_whose_binary_is_built_by_an_earlier_step_counts_as_available(tmp_path):
    ts = T.ToolSet({"run": ToolRole(cmd="{workdir}/simv +TC={tc}"), "lint": ToolRole(cmd="definitely-not-installed-xyz {top}")},
                   T.ToolEnv(), 60)
    assert T.available(ts, "run") and not T.available(ts, "lint") and not T.available(ts, "nope")


def test_tools_init_writes_a_preset_and_check_says_what_is_missing(tmp_path):
    from click.testing import CliRunner

    from q3tui.core.cli import main

    (tmp_path / "q3tui.yaml").write_text("pipeline: {flow: vlsit}\n")
    (tmp_path / "q3tui.yaml").write_text("pipeline: {flow: vlsit}\ntools:\n  roles:\n    sim: {cmd: 'echo {top}'}\n")  # (partly configured)
    r = CliRunner().invoke(main, ["-C", str(tmp_path), "tools", "check"])
    assert r.exit_code == 1 and "lint: not configured" in r.output and "tools init" in r.output
    (tmp_path / "q3tui.yaml").write_text("pipeline: {flow: vlsit}\n")
    r = CliRunner().invoke(main, ["-C", str(tmp_path), "tools", "init"])
    assert r.exit_code == 0 and (tmp_path / "tools.json").is_file()
    roles = __import__("json").loads((tmp_path / "tools.json").read_text())["roles"]
    assert set(roles) >= {"lint", "synth", "sim", "run"} and roles["sim"]["supports"] == ["sva"]
    assert CliRunner().invoke(main, ["-C", str(tmp_path), "tools", "init"]).exit_code != 0  # never overwrites silently
    assert CliRunner().invoke(main, ["-C", str(tmp_path), "tools", "init", "--force"]).exit_code == 0
    assert T.presets() == ["synopsys"]


def test_with_no_tools_file_anywhere_the_synopsys_preset_is_used(tmp_path):
    """Real runs kept stopping at "no tool for role 'lint'" in projects without a tools.json."""
    ts = T.load_tools(cfg(), tmp_path)
    assert set(ts.roles) >= {"lint", "synth", "sim", "run"} and "built-in synopsys preset" in ts.source
    assert ts.roles["lint"].cmd.startswith("vcs ") and ts.roles["sim"].supports == ["sva"]
    (tmp_path / "tools.json").write_text(json.dumps({"roles": {"lint": {"cmd": "mylint {top}", "parse": "generic"}}}))
    ts = T.load_tools(cfg(), tmp_path)  # a file of the project wins; the preset is not merged into it
    assert set(ts.roles) == {"lint"} and "built-in" not in ts.source
    ts = T.load_tools(cfg(roles={"synth": {"cmd": "x {script}"}}), tmp_path / "other")  # inline roles count as configured too
    assert set(ts.roles) == {"synth"}
