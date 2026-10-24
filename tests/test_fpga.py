"""The FPGA flow: SDC model / rendering / editing, Vivado script and report parsing, the three steps with a fake tool, the
Ops / assistant API, and the SDC tab."""

import json
import shutil
import sys
from pathlib import Path

import pytest

from q3tui import flows
from q3tui.core.ops import Ops
from q3tui.core.project import Project
from q3tui.pipeline.engine import Engine, EngineError
from q3tui.flows.fpga.lib import sdc, vivado

DATA = Path(__file__).parent / "data" / "fpga"
RTL = """module blk (input i_clk, input i_rst_n, input [3:0] i_a, input i_v, output reg [3:0] o_y, output o_r);
  always @(posedge i_clk or negedge i_rst_n) if (!i_rst_n) o_y <= 0; else if (i_v) o_y <= i_a;
  assign o_r = 1'b1;
endmodule
"""
PORTS = [{"name": "i_clk", "dir": "input", "vector": False}, {"name": "i_rst_n", "dir": "input", "vector": False},
         {"name": "i_a", "dir": "input", "vector": True}, {"name": "i_v", "dir": "input", "vector": False},
         {"name": "o_y", "dir": "output", "vector": True},
         {"name": "o_r", "dir": "output", "vector": False}]


@pytest.fixture
def proj(tmp_path):
    (tmp_path / "src" / "rtl").mkdir(parents=True)
    (tmp_path / "src" / "rtl" / "blk.sv").write_text(RTL)
    (tmp_path / "src" / "rtl" / "filelist.f").write_text("src/rtl/blk.sv\n")
    (tmp_path / "schemas").mkdir()
    (tmp_path / "schemas" / "structured_spec.json").write_text(json.dumps({"metadata": {"top_module": "blk"}}))
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec" / "spec.md").write_text("| **Clock** | `i_clk`, target frequency 200 MHz (5 ns period) |\n")
    # a stand-in for Vivado: writes the recorded reports into the work directory
    (tmp_path / "fake_vivado.py").write_text(
        "import shutil, sys\nfrom pathlib import Path\n"
        f"for n in ('utilization', 'timing', 'check_timing', 'paths'):\n    shutil.copy(Path({str(DATA)!r}) / (n + '.rpt'), Path(sys.argv[2]) / (n + '.rpt'))\n"
        "print('Vivado v.2025.1 (lin64)')\nprint('WARNING: [Synth 8-3331] design has unconnected port')\n")
    (tmp_path / "q3tui.yaml").write_text(
        "pipeline: {flow: fpga}\ntools:\n  roles:\n    fpga_synth:\n"
        f"      cmd: \"{sys.executable} {tmp_path / 'fake_vivado.py'} {{script}} {{workdir}}\"\n      parse: vivado\n")
    return Project.open(tmp_path)


def _rows(ops):
    cfg = sdc.load(ops.project)
    return sdc.rows(cfg) if cfg else []


# -- SDC --------------------------------------------------------------------------------------------------------------


def test_period_from_the_spec():
    assert sdc.period_from_spec("| **Clock** | target frequency 100 MHz (10 ns period) |") == 10.0
    assert sdc.period_from_spec("The clock runs at 1.25 GHz") == 0.8
    assert sdc.period_from_spec("clock period 4.5 ns") == 4.5
    assert sdc.period_from_spec("the data width is 32 bits at 100 MHz") is None  # no mention of the clock


def test_default_config_and_files():
    cfg = sdc.default_config(PORTS, "clock frequency 200 MHz")
    assert [(c.name, c.period_ns) for c in cfg.clocks] == [("i_clk", 5.0)] and cfg.resets == ["i_rst_n"]
    files = sdc.render(cfg, PORTS)
    assert set(files) == set(sdc.FILES)
    assert "create_clock -name i_clk -period 5 [get_ports i_clk]" in files["clocks.tcl"]
    assert "set_false_path -from [get_ports i_rst_n]" in files["exceptions.tcl"]
    io = files["io_constraints.tcl"]
    assert "set_input_delay -clock [get_clocks i_clk] -max 1.5 [get_ports {i_a[*] i_v}]" in io
    assert "set_output_delay -clock [get_clocks i_clk] -max 1.5 [get_ports {o_y[*] o_r}]" in io
    assert "i_clk" not in io.replace("[get_clocks i_clk]", "")  # the clock port gets no I/O delay
    # a reset is a false path, but its port still gets an input delay (CDC tools call a port with none "partially constrained")
    assert "set_input_delay -clock [get_clocks i_clk] -max 1.5 [get_ports {i_rst_n}]" in io and "-min 0 [get_ports {i_rst_n}]" in io
    assert "(none)" in files["extra.tcl"] and "source $_d/extra.tcl" in files["sdc.tcl"]
    assert "single clock domain" in files["clock_groups.tcl"]
    assert "source $_d/clocks.tcl" in files["sdc.tcl"]


def test_editing_the_constraints():
    cfg = sdc.default_config(PORTS, "")
    assert cfg.clocks[0].period_ns == 10.0  # nothing in the spec: 10 ns
    sdc.apply_edit(cfg, "clock.i_clk.freq_mhz", "250")
    assert cfg.clocks[0].period_ns == 4.0
    sdc.apply_edit(cfg, "io.input_pct", "40")
    sdc.apply_edit(cfg, "env.load_pf", "0.5")
    assert "-max 1.6" in sdc.render(cfg, PORTS)["io_constraints.tcl"] and "set_load 0.5" in sdc.render(cfg, PORTS)["env.tcl"]
    sdc.add_item(cfg, "clock", "clk2 i_clk2 8")
    sdc.add_item(cfg, "group", "asynchronous i_clk | clk2")
    sdc.add_item(cfg, "exception", "multicycle from=i_clk to=clk2 value=3 slow path")
    out = sdc.render(cfg, PORTS)
    assert "set_clock_groups -asynchronous -group [get_clocks {i_clk}] -group [get_clocks {clk2}]" in out["clock_groups.tcl"]
    assert "set_multicycle_path 3 -setup -from [get_clocks i_clk] -to [get_clocks clk2]" in out["exceptions.tcl"]
    assert "set_multicycle_path 2 -hold" in out["exceptions.tcl"]
    sdc.remove_item(cfg, "clock.clk2")
    assert not cfg.clock_groups and len(cfg.clocks) == 1
    with pytest.raises(ValueError, match="positive"):
        sdc.apply_edit(cfg, "clock.i_clk.freq_mhz", "0")
    with pytest.raises(ValueError, match="not a number"):
        sdc.apply_edit(cfg, "clock.i_clk.period_ns", "fast")
    with pytest.raises(ValueError, match="between 0 and 100"):
        sdc.apply_edit(cfg, "io.output_pct", "150")
    with pytest.raises(ValueError, match="last clock"):
        sdc.remove_item(cfg, "clock.i_clk")
    with pytest.raises(ValueError, match="needs from="):
        sdc.add_item(cfg, "exception", "false_path")  # nothing to apply it to


def test_the_sdc_must_match_the_ports_of_the_top():
    cfg = sdc.default_config(PORTS, "")
    assert sdc.port_problems(cfg, PORTS) == []
    renamed = [dict(p, name=p["name"] + "_core") if p["name"] in ("i_clk", "i_rst_n") else p for p in PORTS]  # what the RTL step did
    problems = sdc.port_problems(cfg, renamed)
    assert problems[0].startswith("clock i_clk: port 'i_clk' is not an input (inputs: ") and "i_clk_core" in problems[0]
    assert problems[1] == "reset 'i_rst_n' is not an input"
    cfg.io.exclude = ["nope"]
    assert "io.exclude 'nope' is not a port" in sdc.port_problems(cfg, PORTS)


async def test_the_flow_stops_when_the_rtl_renames_the_clock(proj):
    eng = Engine(proj)
    await eng.run(yes=True)
    rtl = proj.root / "src" / "rtl" / "blk.sv"
    rtl.write_text(rtl.read_text().replace("i_clk", "i_clk_core").replace("i_rst_n", "i_rst_n_core"))
    eng = Engine(proj)
    await eng.run(yes=True)
    st = {v.name: v for v in eng.status()}
    assert st["sdc"].status == "failed" and "port 'i_clk' is not an input" in st["sdc"].detail and "sdc_set key=clock.<name>.port" in st["sdc"].detail
    ops = Ops(eng)
    ops.run_action("sdc_set", key="clock.i_clk.port", value="i_clk_core")  # (the fix: the clock keeps its name, the port changes)
    ops.run_action("sdc_set", key="resets", value="i_rst_n_core")
    eng = Engine(proj)
    await eng.run(yes=True)
    assert {v.status for v in eng.status()} == {"done"}
    assert "create_clock -name i_clk -period 5 [get_ports i_clk_core]" in (proj.root / "constraints" / "sdc" / "clocks.tcl").read_text()


def test_replacing_an_item_keeps_its_place_and_an_invalid_one_changes_nothing():
    cfg = sdc.default_config(PORTS, "")
    sdc.add_item(cfg, "exception", "false_path from=i_a to=o_y")
    sdc.add_item(cfg, "exception", "max_delay from=i_v to=o_r value=2")
    sdc.replace_item(cfg, "exception.0", "multicycle from=i_a to=o_y value=3")
    assert [e.kind for e in cfg.exceptions] == ["multicycle", "max_delay"] and cfg.exceptions[0].value == 3
    with pytest.raises(ValueError, match="needs value"):
        sdc.replace_item(cfg, "exception.1", "min_delay from=i_v to=o_r")
    assert [e.kind for e in cfg.exceptions] == ["multicycle", "max_delay"]
    with pytest.raises(ValueError, match="only an exception"):
        sdc.replace_item(cfg, "clock.i_clk", "x")


# -- Vivado ---------------------------------------------------------------------------------------------------------


def test_vivado_reports():
    util = vivado.parse_utilization((DATA / "utilization.rpt").read_text())
    assert util["lut"] == {"used": 759, "available": 203800, "pct": 0.37} and util["ff"]["used"] == 1490 and util["dsp"]["used"] == 0
    t = vivado.parse_timing((DATA / "timing.rpt").read_text())
    assert t["wns_ns"] == 3.639 and t["tns_ns"] == 0.0 and t["endpoints"] == 3094 and t["failing_endpoints"] == 0
    assert t["whs_ns"] == -0.181 and t["ths_ns"] == -125.872 and t["hold_failing_endpoints"] == 740 and not t["met"]  # (hold, before placement)
    cp = vivado.critical_path((DATA / "paths.rpt").read_text())
    assert cp["slack_ns"] == 3.639 and cp["logic_levels"] == 4 and cp["destination"] == "o_sram_addr[0][0]"
    assert vivado.unconstrained((DATA / "check_timing.rpt").read_text()) == {"no_input_delay": 1}


def test_vivado_script(tmp_path):
    (tmp_path / "clocks.tcl").write_text("x")
    (tmp_path / "exceptions.tcl").write_text("x")
    text = vivado.script_text([Path("a.sv"), Path("b.sv")], "top", tmp_path, tmp_path / "w",
                              {"fpga": {"part": "xc7a100tcsg324-1", "implement": True, "generics": {"W": 8}}})
    lines = text.splitlines()
    assert lines[1] == 'read_verilog -sv [list "a.sv" "b.sv"]'
    assert [l for l in lines if l.startswith("read_xdc")] == [f'read_xdc -mode out_of_context "{tmp_path / "clocks.tcl"}"',
                                                              f'read_xdc -mode out_of_context "{tmp_path / "exceptions.tcl"}"']
    assert "synth_design -top top -part xc7a100tcsg324-1 -mode out_of_context -generic W=8" in text
    assert lines.index("place_design") > lines.index("opt_design") and "route_design" in text
    assert "synth_design" in text.split("place_design")[0]


def test_vivado_log_parser():
    from q3tui.eda.tools import parse_log

    d = parse_log("vivado", "ERROR: [Synth 8-439] module 'x' not found\nCRITICAL WARNING: [Constraints 18-4427] bad\nWARNING: [Synth 8-3331] unconnected\nINFO: ok\n")
    assert [(x.severity, x.code) for x in d] == [("error", "Synth 8-439"), ("warning", "CRITICAL WARNING"), ("warning", "Synth 8-3331")]


def test_the_fpga_role_comes_only_from_the_projects_tools_file(tmp_path):
    from q3tui.core.config import Config
    from q3tui.eda.tools import load_tools, preset_data, roles_needed, write_preset

    assert load_tools(Config(), tmp_path).roles == {}  # no flow default, no fallback: not configured
    assert {"fpga_synth", "fpga_yosys"} <= set(roles_needed(flows.load_flow("fpga")))
    write_preset(tmp_path / "config")  # `tools init`: the template carries the FPGA flow's roles too
    ts = load_tools(Config(), tmp_path)
    assert ts.role("fpga_synth").modules == ["xilinx/vivado"] and "lint" in ts.roles and "fpga_pnr_ecp5" in ts.roles
    assert "fpga_synth" in preset_data()["roles"]


# -- the flow -------------------------------------------------------------------------------------------------------


def test_flow_loads():
    flow = flows.load_flow("fpga", Path.cwd())
    assert [s.id for s in flow.steps] == ["sdc", "fpga_synth", "timing_util_report", "ppa_optimize"]
    assert flow.gates == {"timing_util_report": "human", "ppa_optimize": "human"}


async def test_the_steps_run(proj):
    eng = Engine(proj)
    await eng.run(yes=True)
    assert all(v.status == "done" for v in eng.status()), [(v.name, v.status, v.detail) for v in eng.status()]
    cfg = sdc.load(proj)
    assert cfg.clocks[0].period_ns == 5.0  # the spec's 200 MHz
    assert (proj.root / "constraints" / "sdc" / "io_constraints.tcl").is_file() and (proj.root / "build" / "syn" / "fpga" / "synth.tcl").is_file()
    assert 'read_xdc -mode out_of_context' in (proj.root / "build" / "syn" / "fpga" / "synth.tcl").read_text()
    s = json.loads((proj.schemas_dir / "fpga_synth.json").read_text())
    assert s["status"] == "pass" and s["tool"] == "Vivado 2025.1" and s["warnings"] == 1 and s["critical_warnings"] == 0
    ppa = json.loads((proj.schemas_dir / "ppa_report.json").read_text())
    assert ppa["utilization"]["lut"]["used"] == 759 and ppa["timing"]["wns_ns"] == 3.639
    assert ppa["timing"]["fmax_mhz"] == round(1000 / (5.0 - 3.639), 1) and ppa["verdict"]["overall"] == "pass"
    assert ppa["verdict"]["hold"] == "not judged (post-synthesis)" and ppa["timing"]["whs_ns"] == -0.181
    assert ppa["unconstrained"] == {"no_input_delay": 1}


async def test_a_slow_tool_does_not_freeze_the_event_loop(proj):
    """The TUI runs the engine on its own event loop: a tool that takes minutes must not block it (the TUI hung on `r`)."""
    import anyio

    fake = proj.root / "fake_vivado.py"
    fake.write_text("import time\n" + fake.read_text().replace("print('Vivado", "time.sleep(1.5)\nprint('Vivado", 1))
    ticks = 0

    async def heartbeat():
        nonlocal ticks
        while True:
            await anyio.sleep(0.05)
            ticks += 1

    eng = Engine(proj)
    async with anyio.create_task_group() as tg:
        tg.start_soon(heartbeat)
        await eng.run(yes=True)
        tg.cancel_scope.cancel()
    assert {v.name: v.status for v in eng.status()}["fpga_synth"] == "done"
    assert ticks > 15, f"the event loop was blocked ({ticks} heartbeats in a run with a 1.5 s tool)"


async def test_vivados_progress_reaches_the_log_and_stop_kills_it(proj):
    """While Vivado runs its phases are shown (it writes its log live); stopping the run (x) must not leave it running."""
    import anyio
    import subprocess

    fake = proj.root / "fake_vivado.py"
    fake.write_text(
        "import sys, time\nfrom pathlib import Path\nlog = Path(sys.argv[2]) / 'vivado.log'\n"
        "for line in ('Starting RTL Elaboration : Time (s): cpu = 00:00:03 ; elapsed = 00:00:04 . Memory (MB): peak = 1900',\n"
        "             'INFO: [Synth 8-6157] noise', 'Start Technology Mapping', 'Finished Technology Mapping : Time (s): cpu = 00:00:09'):\n"
        "    with log.open('a') as f:\n        f.write(line + '\\n')\n    time.sleep(1.2)\n"
        "time.sleep(60)\n")
    eng = Engine(proj)
    said: list[str] = []
    eng.bus.subscribe(lambda ev: said.append(ev.data.get("message", "")) if ev.kind == "log" else None)
    with anyio.move_on_after(7) as scope:  # the run is stopped like `x` does: cancelled
        await eng.run(yes=True)
    assert scope.cancelled_caught
    progress = [m for m in said if m.startswith("vivado:")]
    assert progress[:3] == ["vivado: Starting RTL Elaboration", "vivado: Start Technology Mapping", "vivado: Finished Technology Mapping"]
    assert not any("noise" in m for m in said)  # only the phases, not every line
    await anyio.sleep(1.5)
    left = subprocess.run(["pgrep", "-f", str(proj.root / "build" / "syn" / "fpga" / "synth.tcl")], capture_output=True, text=True).stdout.split()
    assert left == [], "the tool was left running after the stop"


async def test_a_report_without_timing_says_why(proj):
    eng = Engine(proj)
    await eng.run(yes=True)
    rpt = proj.root / "build" / "syn" / "fpga" / "reports" / "timing.rpt"
    text = rpt.read_text()
    row = [l for l in text.splitlines() if l.strip().startswith("3.639")][0]
    rpt.write_text(text.replace(row, "         NA           NA                     NA                   NA           NA           NA                     NA                   NA           NA           NA                      NA                    NA  "))
    from q3tui.pipeline.base import StepFailed

    with pytest.raises(StepFailed, match="no clock constraint matched"):
        await eng.run_step(eng.by_name["timing_util_report"])
    assert {v.name: v for v in eng.status()}["timing_util_report"].status == "failed"


def test_phase_lines():
    from q3tui.flows.fpga.lib.vivado import phase_line

    assert phase_line("Starting RTL Elaboration : Time (s): cpu = 00:00:03 ; elapsed = 00:00:04 . Memory (MB): peak = 1900") == "Starting RTL Elaboration"
    assert phase_line("Finished Cross Boundary and Area Optimization : Time (s): cpu = 00:00:09") == "Finished Cross Boundary and Area Optimization"
    assert phase_line("Synthesis finished with 0 errors, 0 critical warnings and 2 warnings.") == "Synthesis finished with 0 errors, 0 critical warnings and 2 warnings."
    assert phase_line("INFO: [Synth 8-6157] synthesizing module 'x'") is None and phase_line("") is None


async def test_targets_are_judged(proj):  # (the report only: the gate stops the run before ppa_optimize)
    (proj.root / "flows").mkdir()
    shutil.copytree(flows.BUILTIN_DIR / "fpga", proj.root / "flows" / "fpga", ignore=shutil.ignore_patterns("__pycache__"))
    skill = proj.root / "flows" / "fpga" / "timing_util_report" / "md" / "skill.md"
    skill.write_text(skill.read_text().replace("options: {}", "options:\n  targets: {max_util_pct: {ff: 0.1}, min_slack_ns: 5}"))
    eng = Engine(proj)
    await eng.run()
    v = json.loads((proj.schemas_dir / "ppa_report.json").read_text())["verdict"]
    assert v["timing"] == "fail" and v["utilization"] == "fail" and v["overall"] == "fail" and "ff" in v["over_budget"]


async def test_editing_the_sdc_makes_the_synthesis_stale(proj):
    eng = Engine(proj)
    await eng.run(yes=True)
    ops = Ops(eng)
    assert any(k == "clock.i_clk.freq_mhz" and v == "200" for k, _, v in _rows(ops))
    msg = ops.run_action("sdc_set", key="clock.i_clk.freq_mhz", value="400")
    assert "2.5 ns" in msg and "create_clock -name i_clk -period 2.5" in (proj.root / "constraints" / "sdc" / "clocks.tcl").read_text()
    st = {v.name: v.status for v in Engine(proj).status()}
    assert st["sdc"] == "stale" and st["fpga_synth"] == "stale"
    with pytest.raises(EngineError, match="positive"):
        ops.run_action("sdc_set", key="clock.i_clk.freq_mhz", value="-5")
    assert "400" in json.dumps(json.loads((proj.root / "constraints" / "sdc" / "sdc.json").read_text()) and _rows(ops))
    ops.run_action("sdc_add", what="exception", text="false_path from=i_v to=o_y")
    assert "set_false_path -from [get_ports i_v] -to [get_ports o_y]" in (proj.root / "constraints" / "sdc" / "exceptions.tcl").read_text()
    ops.run_action("sdc_remove", key="exception.0")
    assert "i_v" not in (proj.root / "constraints" / "sdc" / "exceptions.tcl").read_text().replace("i_vld", "")
    e2 = Engine(proj)
    await e2.run(yes=True)  # the sdc step renders again and the synthesis follows
    assert {v.status for v in e2.status()} == {"done"}


def test_no_sdc_step_in_another_flow(tmp_path):
    from tests.fakes import use_test_flow

    use_test_flow(tmp_path)
    with pytest.raises(EngineError, match="no action"):
        Ops(Engine(Project.open(tmp_path))).run_action("sdc_set", key="io.input_pct", value="10")


# -- the SDC tab ------------------------------------------------------------------------------------------------------


async def test_sdc_view_has_three_columns(proj):
    import sys

    from textual.widgets import DataTable, Input

    from q3tui.tui.app import Q3TUIApp

    eng = Engine(proj)
    await eng.run(yes=True)
    app = Q3TUIApp(proj)
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause()
        # (the flow's panels.py is loaded by path, so its classes are found by name)
        (ed,) = [w for w in app.query("SdcEditor")]
        mod = sys.modules[type(ed).__module__]
        sections = {s.id: s for s in mod.SECTIONS}
        assert list(sections) == ["clocks", "groups", "io", "exceptions", "env"]  # the order of the files
        sec_table, items = ed.query_one("#sdc-sections", DataTable), ed.query_one("#sdc-items", DataTable)  # column 1, column 2
        ed.refresh_view()
        assert [sec_table.get_row_at(i)[0] for i in range(sec_table.row_count)] == ["Clocks", "Clock groups", "I/O", "Exceptions", "Environment"]
        (row,) = sections["clocks"].rows(app)
        assert row[:4] == ("clock.i_clk", "i_clk", "i_clk", "200") and str(row[4]) == "same" and row[5:8] == ("5", "0", "50")
        assert str(row[8]) == "–"  # (the ASIC flow has not run: no hint)
        assert [r[0] for r in sections["io"].rows(app)] == ["io.input_pct", "io.output_pct", "io.clock", "io.exclude"]
        assert [r[0] for r in sections["exceptions"].rows(app)] == ["resets"] and str(sections["exceptions"].rows(app)[0][2]) == "i_rst_n"
        assert [r[0] for r in sections["env"].rows(app)] == ["env.driving_cell", "env.load_pf", "env.max_fanout", "env.max_transition_ns"]
        assert sections["groups"].rows(app) == []
        assert items.row_count == 1 and ed.spec.id == "clocks"  # column 2 shows the highlighted section's items

        # moving in column 1 changes column 2 (and its columns)
        sec_table.move_cursor(row=2)
        await pilot.pause()
        assert ed.spec.id == "io" and items.row_count == 4 and [str(c.label) for c in items.columns.values()] == ["Setting", "Value"]
        sec_table.move_cursor(row=0)
        await pilot.pause()
        assert ed.spec.id == "clocks" and len(items.columns) == 8

        # changes go through the actions; every section follows
        app.ops.run_action("sdc_add", what="clock", text="clk2 i_clk 8")
        app.ops.run_action("sdc_add", what="group", text="asynchronous i_clk | clk2")
        app.ops.run_action("sdc_add", what="exception", text="false_path from=i_v to=o_y")
        assert [r[1] for r in sections["clocks"].rows(app)] == ["i_clk", "clk2"]
        assert sections["groups"].rows(app) == [("group.0", "asynchronous", "i_clk | clk2")]
        assert [r[0] for r in sections["exceptions"].rows(app)] == ["resets", "exception.0"]
        # column 3 is the section's generated file
        assert "create_clock -name clk2 -period 8" in sections["clocks"].detail(app, "clock.clk2")
        assert "set_clock_groups -asynchronous" in sections["groups"].detail(app, "group.0")

        # e opens a dialog with the fields of the highlighted item: edit the clock just added (a new row is selected)
        from textual.widgets import Select

        async def dialog(**values):
            await pilot.pause()
            for k, v in values.items():
                w = app.screen.query_one(f"#f-{k}")
                if isinstance(w, Select):
                    w.value = v
                else:
                    w.value = v
            await pilot.press("ctrl+s")
            await pilot.pause()

        ed.refresh_view()
        ed.action_edit()
        await dialog(freq_mhz="250", uncertainty_ns="0.2")
        assert (sections["clocks"].rows(app)[1][1:4], sections["clocks"].rows(app)[1][5:7]) == (("clk2", "i_clk", "250"), ("4", "0.2")) and sections["clocks"].rows(app)[0][3] == "200"
        # a: a new clock from name / port / frequency
        ed.action_add()
        await dialog(name="clk3", port="i_clk3", mhz="100")
        assert [r[1:4] for r in sections["clocks"].rows(app)][-1] == ("clk3", "i_clk3", "100")
        # I/O: one dialog for the whole budget
        sec_table.move_cursor(row=2)
        await pilot.pause()
        ed.action_edit()
        await dialog(io_input_pct="45", io_exclude="i_v")
        assert dict((r[0], str(r[2])) for r in sections["io"].rows(app)) == {"io.input_pct": "45", "io.output_pct": "30", "io.clock": "i_clk", "io.exclude": "i_v"}
        # exceptions: the reset list, a new exception, and editing one
        sec_table.move_cursor(row=3)
        await pilot.pause()
        ed.action_add()
        await dialog(kind="multicycle", **{"from": "i_clk", "to": "clk2"}, value="3", note="slow path")
        rows = sections["exceptions"].rows(app)
        assert [r[0] for r in rows] == ["resets", "exception.0", "exception.1"] and rows[2][1:] == ("multicycle", "i_clk", "clk2", "–", "3")
        items.move_cursor(row=2)
        await pilot.pause()
        ed.action_edit()
        await dialog(value="5")
        assert sections["exceptions"].rows(app)[2][5] == "5"
        # clock groups: add through the dialog, then edit it (a choice and a text field)
        sec_table.move_cursor(row=1)
        await pilot.pause()
        ed.action_add()
        await dialog(kind="logically_exclusive", groups="i_clk | clk2")
        assert sections["groups"].rows(app) == [("group.0", "asynchronous", "i_clk | clk2"), ("group.1", "logically_exclusive", "i_clk | clk2")]
        items.move_cursor(row=0)
        await pilot.pause()
        ed.action_edit()
        await dialog(kind="physically_exclusive")
        assert sections["groups"].rows(app)[0] == ("group.0", "physically_exclusive", "i_clk | clk2")
        # the bars between the columns are the app's splitters: resizable, remembered per project, reset by a double-click
        from q3tui.tui.splitter import Splitter

        split1 = ed.query_one("#sdc-split-1", Splitter)
        assert split1.pane is sec_table and ed.query_one("#sdc-split-2", Splitter).pane is items
        before = sec_table.outer_size.width
        split1.set_size(before + 8)
        await pilot.pause()
        assert sec_table.outer_size.width == before + 8
        app.remember_layout("sdc-split-1", sec_table.outer_size.width)
        assert json.loads(app.layout_file.read_text())["sdc-split-1"] == before + 8
        split1.set_size(None)
        await pilot.pause()
        assert sec_table.outer_size.width == before  # (double-click: back to the default)
        # d removes in the groups section
        sec_table.move_cursor(row=1)
        await pilot.pause()
        ed.refresh_view()
        while sections["groups"].rows(app):
            ed.action_remove()
        assert sections["groups"].rows(app) == []


# -- ppa_optimize and the hand-off ------------------------------------------------------------------------------------


def _fork(proj, **skill_edits):
    """Copy the fpga flow into the project and edit step skill files: {step: (old, new)}."""
    dest = proj.root / "flows" / "fpga"
    (proj.root / "flows").mkdir(exist_ok=True)
    shutil.copytree(flows.BUILTIN_DIR / "fpga", dest, ignore=shutil.ignore_patterns("__pycache__"))
    for step, (old, new) in skill_edits.items():
        f = dest / step / "md" / "skill.md"
        assert old in f.read_text(), (step, old)
        f.write_text(f.read_text().replace(old, new, 1))


class PlanLLM:
    def __init__(self, plan):
        self.plan, self.stages = plan, []

    async def __call__(self, stage, cfg, emit):
        from q3tui.llm.runtime import StageResult

        self.stages.append(stage)
        emit.add_cost(0.01)
        return StageResult("ok", self.plan, 0.01, 2, "s")


def _plan():
    from q3tui.flows.fpga.lib.plan import Fix, PpaPlan

    return PpaPlan(analysis="the write path has 4 logic levels", fixes=[
        Fix(target="rtl", module="blk", change="register the address before the bank decode", why="worst path o_y", expected="-1 level"),
        Fix(target="rtl", module="ghost", change="x", why="y"),
        Fix(target="spec", change="the clock target is out of reach for this part", why="WNS -2 ns")])


async def test_nothing_to_optimize_when_the_targets_are_met(proj, monkeypatch):
    from q3tui.llm import runtime

    fake = PlanLLM(_plan())
    monkeypatch.setattr(runtime, "run_stage", fake)
    eng = Engine(proj)
    await eng.run(yes=True)
    assert {v.name: v.status for v in eng.status()}["ppa_optimize"] == "done" and fake.stages == []  # no LLM
    plan = json.loads((proj.schemas_dir / "ppa_plan.json").read_text())
    assert plan["status"] == "met" and plan["fixes"] == []


async def test_a_missed_target_gets_proposals_from_a_read_only_stage(proj, monkeypatch):
    from q3tui.llm import runtime

    _fork(proj, timing_util_report=("options: {}", "options:\n  targets: {min_slack_ns: 5}"))
    fake = PlanLLM(_plan())
    monkeypatch.setattr(runtime, "run_stage", fake)
    eng = Engine(proj)
    await eng.run(yes=True)
    (stage,) = fake.stages
    assert set(stage.builtin_tools) == {"Read", "Grep", "Glob"} and stage.write_dirs is None and stage.output_model.__name__ == "PpaPlan"
    assert "setup slack WNS 3.639 ns is below the target 5" in stage.prompt and "RTL modules" in stage.prompt and "blk" in stage.prompt
    assert any(d.name == "tb" for d in stage.deny_dirs) and any(d.name == "sva" for d in stage.deny_dirs)  # independence
    plan = json.loads((proj.schemas_dir / "ppa_plan.json").read_text())
    assert plan["status"] == "proposed" and [f["target"] for f in plan["fixes"]] == ["rtl", "spec"]  # the unknown module is dropped
    assert plan["dropped"] == ["fix 2: unknown module 'ghost'"] and plan["fixes"][0]["id"] == "F1" and plan["fixes"][1]["id"] == "F3"


async def test_handoff_sends_change_requests_to_the_owning_flow(proj, monkeypatch):
    from q3tui.llm import runtime

    (proj.root / "flows").mkdir(exist_ok=True)
    (proj.root / "flows" / "owner.yaml").write_text("name: owner\nsteps:\n  - {id: spec, kind: spec}\n  - {id: rtl, kind: reads, deps: [spec]}\n")
    _fork(proj, timing_util_report=("options: {}", "options:\n  targets: {min_slack_ns: 5}"),
          ppa_optimize=("handoff_flow: rtl", "handoff_flow: owner"))
    monkeypatch.setattr(runtime, "run_stage", PlanLLM(_plan()))
    eng = Engine(proj)
    await eng.run(yes=True)
    ops = Ops(eng)
    msg = ops.run_action("ppa_handoff", ids="F1")
    assert "F1 → owner:rtl" in msg and "q3tui --flow owner run" in msg
    other = Engine(proj, flow=flows.load_flow("owner", proj.root))
    (req,) = other.state.feedback["rtl"]
    assert req.startswith("[rtl:blk] PPA (xc7k325tffg900-2): register the address before the bank decode.") and "worst path o_y" in req
    assert "spec" not in other.state.feedback  # only the fix asked for
    ops.run_action("ppa_handoff")  # the rest: the spec fix
    other = Engine(proj, flow=flows.load_flow("owner", proj.root))
    assert other.state.feedback["spec"][0].startswith("PPA (xc7k325tffg900-2): the target cannot be met by RTL changes alone. the clock target")
    plan = json.loads((proj.schemas_dir / "ppa_plan.json").read_text())
    assert [f["handed_off"]["step"] for f in plan["fixes"]] == ["rtl", "spec"]
    with pytest.raises(EngineError, match="no proposed fix"):
        ops.run_action("ppa_handoff")
    # this flow's own state survived the other flow's writes, and handing off is not an edit of the plan's step
    st = {v.name: v for v in Engine(proj).status()}
    assert st["fpga_synth"].status == "done" and st["ppa_optimize"].edited == []


async def test_the_rounds_are_bounded_and_remembered(proj, monkeypatch):
    from q3tui.llm import runtime

    _fork(proj, timing_util_report=("options: {}", "options:\n  targets: {min_slack_ns: 5}"),
          ppa_optimize=("max_iterations: 3", "max_iterations: 1"))
    fake = PlanLLM(_plan())
    monkeypatch.setattr(runtime, "run_stage", fake)
    eng = Engine(proj)
    await eng.run(yes=True)
    Ops(eng).run_action("ppa_handoff")
    # the RTL changed (the owner flow ran): synthesis and the report are stale, the optimizer runs a second round
    (proj.root / "src" / "rtl" / "blk.sv").write_text(RTL.replace("o_r = 1'b1", "o_r = 1'b0"))
    eng = Engine(proj)
    await eng.run(yes=True)
    plan = json.loads((proj.schemas_dir / "ppa_plan.json").read_text())
    assert plan["status"] == "limit" and len(plan["history"]) == 1 and plan["history"][0]["fixes"][0]["module"] == "blk"
    assert len(fake.stages) == 1  # the limit stopped a second LLM call
    assert {v.name: v.status for v in eng.status()}["ppa_optimize"] == "failed"
    # a new clock is a new problem: its rounds are counted afresh (the earlier ones stay as context, with their clock)
    Ops(eng).run_action("sdc_set", key="clock.i_clk.freq_mhz", value="300")
    eng = Engine(proj)
    await eng.run(yes=True)
    assert len(fake.stages) == 2 and "round 1 at i_clk 200 MHz" in fake.stages[1].prompt
    plan = json.loads((proj.schemas_dir / "ppa_plan.json").read_text())
    assert plan["status"] == "proposed" and plan["clocks"] == "i_clk 300 MHz" and len(plan["history"]) == 1
    assert {v.name: v.status for v in eng.status()}["ppa_optimize"] == "done"


def test_settings_follow_the_flows_steps(proj):
    ops = Ops(Engine(proj))
    tabs = {t.id: t for t in ops.settings_tabs()}
    assert not {"spec", "rtl", "tb"} & set(tabs)  # the spec tabs are not this flow's
    assert [s.path for s in tabs["models"].settings][::2] == [
        f"llm.steps.{k}.model" for k in ("sdc", "fpga_synth", "timing_util_report", "ppa_optimize", "assistant")]
    assert "pipeline.parallel_rtl_tb" not in {s.path for t in tabs.values() for s in t.settings}
    assert "llm.steps.ppa_optimize.model" in ops.settings()
    ops.apply_settings({"llm.steps.ppa_optimize.model": "claude-sonnet-5-5"})
    assert proj.cfg.llm.for_stage("ppa_optimize", "propose").model == "claude-sonnet-5-5"
    assert proj.cfg.llm.for_stage("sdc", "x").model == proj.cfg.llm.model


async def test_the_settings_dialog_shows_this_flows_steps(proj):
    from textual.widgets import TabPane

    from q3tui.tui.app import Q3TUIApp
    from q3tui.tui.screens import SettingsScreen

    app = Q3TUIApp(proj)
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause()
        app.action_settings()
        await pilot.pause()
        assert isinstance(app.screen, SettingsScreen)
        ids = {p.id for p in app.screen.query(TabPane)}
        assert {"set-tab-general", "set-tab-llm", "set-tab-models", "set-tab-gates"} <= ids
        assert not ids & {"set-tab-spec", "set-tab-rtl", "set-tab-tb"}
        assert app.screen.query("#set-llm-steps-ppa_optimize-model")


async def test_switching_flows_from_the_tui(proj):
    from q3tui.core.config import load_config
    from q3tui.tui.app import Q3TUIApp

    app = Q3TUIApp(proj)
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause()
        assert "spec" in app._completions("flow") and "fpga" in app._completions("flow")
        app.action_switch_flow("fpga")  # already there: nothing happens
        await pilot.pause()
        assert app.return_value is None
        app.action_switch_flow("nope")  # unknown: stays, says why
        await pilot.pause()
        assert app.return_value is None and "flow: fpga" in (proj.root / "q3tui.yaml").read_text()
        app.action_switch_flow("spec")
        await pilot.pause()
    assert app.return_value == ("flow", "spec")
    assert load_config(None, cwd=proj.root).pipeline.flow == "spec"


async def test_the_flow_map_picks_the_flow_of_a_box(proj):
    from q3tui.tui.app import Q3TUIApp
    from q3tui.tui.flowmap import FlowMapScreen, Group

    app = Q3TUIApp(proj)
    async with app.run_test(size=(150, 60)) as pilot:
        await pilot.pause()
        app.action_switch_flow()
        await pilot.pause()
        assert isinstance(app.screen, FlowMapScreen)
        by_title = {g.border_title.split("  ·")[0]: g for g in app.screen.query(Group)}
        assert {t for t, g in by_title.items() if g.developed} == {"Spec", "RTL gen", "FPGA synthesis", "ASIC synthesis", "Sanity", "Project settings"}
        assert by_title["DV"].has_class("off") and by_title["FPGA synthesis"].has_class("current")
        app.screen.pick(by_title["DV"])  # not developed: stays open
        await pilot.pause()
        assert isinstance(app.screen, FlowMapScreen)
        app.screen.pick(by_title["Spec"])  # a box of the spec flow
        await pilot.pause()
    assert app.return_value == ("flow", "spec")


def _owner_flow(proj):
    (proj.root / "flows").mkdir(exist_ok=True)
    (proj.root / "flows" / "owner.yaml").write_text("name: owner\nsteps:\n  - {id: spec, kind: spec}\n  - {id: rtl, kind: reads, deps: [spec]}\n")


async def test_a_human_gate_offers_the_handoff_and_the_switch(proj, monkeypatch):
    from q3tui.llm import runtime
    from q3tui.tui.app import Q3TUIApp
    from q3tui.tui.screens import ConfirmScreen

    _owner_flow(proj)
    _fork(proj, timing_util_report=("options: {}", "options:\n  targets: {min_slack_ns: 5}"),
          ppa_optimize=("handoff_flow: rtl", "handoff_flow: owner"))
    monkeypatch.setattr(runtime, "run_stage", PlanLLM(_plan()))
    eng = Engine(proj)
    await eng.run(yes=True)
    offer = eng.by_name["ppa_optimize"].follow_up(eng)
    assert offer["action"] == "ppa_handoff" and offer["switch"] == "owner" and "F1, F3" in offer["detail"]
    assert eng.by_name["sdc"].follow_up(eng) is None
    # nothing was handed off on a human gate; the TUI asks
    assert Engine(proj, flow=flows.load_flow("owner", proj.root)).state.feedback.get("rtl") is None
    app = Q3TUIApp(proj)
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause()
        app._gate_choice("ppa_optimize", "approve")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        app.screen.dismiss(True)
        await pilot.pause()
    assert app.return_value == ("flow", "owner")
    assert len(Engine(proj, flow=flows.load_flow("owner", proj.root)).state.feedback["rtl"]) == 1


async def test_a_gate_in_mode_auto_hands_off_the_rtl_fixes_only(proj, monkeypatch):
    from q3tui.llm import runtime

    _owner_flow(proj)
    _fork(proj, timing_util_report=("options: {}", "options:\n  targets: {min_slack_ns: 5}"),
          ppa_optimize=("handoff_flow: rtl", "handoff_flow: owner"))
    skill = proj.root / "flows" / "fpga" / "ppa_optimize" / "md" / "skill.md"
    skill.write_text(skill.read_text().replace("gate: human", "gate: auto"))
    monkeypatch.setattr(runtime, "run_stage", PlanLLM(_plan()))
    eng = Engine(proj)
    await eng.run(yes=True)
    other = Engine(proj, flow=flows.load_flow("owner", proj.root))
    assert other.state.feedback["rtl"][0].startswith("[rtl:blk]")
    assert "spec" not in other.state.feedback  # a spec change is not accepted on your behalf
    assert [f["id"] for f in eng.by_name["ppa_optimize"]._pending(eng)] == ["F3"]
    assert not (proj.root / "notes" / "rtl.txt").exists()  # (mode auto does not run the other flow)


async def test_auto_answer_runs_the_owner_flow_and_goes_round_again(proj, monkeypatch):
    from q3tui.llm import runtime

    _owner_flow(proj)
    _fork(proj, timing_util_report=("options: {}", "options:\n  targets: {min_slack_ns: 5}"),
          ppa_optimize=("handoff_flow: rtl", "handoff_flow: owner"))
    skill = proj.root / "flows" / "fpga" / "ppa_optimize" / "md" / "skill.md"
    skill.write_text(skill.read_text().replace("gate: human", "gate: auto_answer"))
    fake = PlanLLM(_plan())
    monkeypatch.setattr(runtime, "run_stage", fake)
    # the owner's rtl run stands in for the RTL update: it changes the RTL, as the real one does
    from tests.fakes import ReadsStep

    plain = ReadsStep.run

    async def run(self, ctx):
        await plain(self, ctx)
        if self.name == "rtl":
            f = ctx.project.root / "src" / "rtl" / "blk.sv"
            f.write_text(f.read_text() + "// pipelined\n")

    monkeypatch.setattr(ReadsStep, "run", run)
    eng = Engine(proj)
    events = []
    eng.bus.subscribe(lambda e: events.append(e))
    outcome = await eng.run(yes=True)
    assert (proj.root / "notes" / "rtl.txt").is_file()  # the owner flow's rtl ran
    assert len(fake.stages) >= 2  # ppa_optimize was asked again after the round
    assert any("running owner:rtl" in str(e.data.get("message", "")) for e in events)
    assert any("need a change of the spec" in str(e.data.get("message", "")) for e in events)
    assert outcome == "failed" and len(fake.stages) == 3  # bounded: three rounds, the targets stay missed with the stand-in
    assert any("optimization round" in str(e.data.get("message", "")) for e in events if e.kind == "error")


async def test_h_in_the_fixes_tab_moves_to_the_owner_flow(proj, monkeypatch):
    from q3tui.llm import runtime
    from q3tui.tui.app import Q3TUIApp

    _owner_flow(proj)
    _fork(proj, timing_util_report=("options: {}", "options:\n  targets: {min_slack_ns: 5}"),
          ppa_optimize=("handoff_flow: rtl", "handoff_flow: owner"))
    monkeypatch.setattr(runtime, "run_stage", PlanLLM(_plan()))
    await Engine(proj).run(yes=True)
    app = Q3TUIApp(proj)
    async with app.run_test(size=(150, 50)) as pilot:
        await pilot.pause()
        app._select_step("ppa_optimize")
        await pilot.pause()
        (tab,) = [w for w in app.query("FixTab")]
        tab.action_handoff()
        await pilot.pause()
    assert app.return_value == ("flow", "owner")
    assert len(Engine(proj, flow=flows.load_flow("owner", proj.root)).state.feedback["rtl"]) == 1


async def test_switching_while_a_step_runs_asks_instead_of_refusing(proj):
    from q3tui.tui.app import Q3TUIApp
    from q3tui.tui.screens import ConfirmScreen

    app = Q3TUIApp(proj)
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause()

        class Busy:
            is_running = True

        app._pipeline_worker = Busy()
        app.engine.running = "fpga_synth"
        app.action_switch_flow("spec")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen) and app.return_value is None
        app.screen.dismiss(False)  # no: stays
        await pilot.pause()
        assert app.return_value is None
        app.action_switch_flow("spec")
        await pilot.pause()
        app.screen.dismiss(True)
        await pilot.pause()
        app.engine.running = None
    assert app.return_value == ("flow", "spec")


async def test_targets_met_on_changed_rtl_run_the_owners_functional_check(proj, monkeypatch):
    from q3tui.llm import runtime

    (proj.root / "flows").mkdir(exist_ok=True)
    (proj.root / "flows" / "owner.yaml").write_text(
        "name: owner\nsteps:\n  - {id: spec, kind: spec}\n  - {id: rtl, kind: reads, deps: [spec]}\n"
        "  - {id: format, kind: reads, deps: [rtl]}\n  - {id: smoke, kind: reads, deps: [format]}\n")
    _fork(proj, timing_util_report=("options: {}", "options:\n  targets: {min_slack_ns: 5}"),
          ppa_optimize=("handoff_flow: rtl", "handoff_flow: owner"))
    fake = PlanLLM(_plan())
    monkeypatch.setattr(runtime, "run_stage", fake)
    eng = Engine(proj)
    await eng.run(yes=True)  # round 1: proposals
    Ops(eng).run_action("ppa_handoff", ids="F1")
    await Engine(proj, flow=flows.load_flow("owner", proj.root)).run(only="rtl", yes=True)  # (the owner's rtl, as its user ran it)
    (proj.root / "src" / "rtl" / "blk.sv").write_text(RTL.replace("o_r = 1'b1", "o_r = 1'b0"))  # ... and it changed the RTL
    flow_dir = proj.root / "flows" / "fpga"
    t = flow_dir / "timing_util_report" / "md" / "skill.md"
    t.write_text(t.read_text().replace("targets: {min_slack_ns: 5}", "targets: {}"))  # now the targets are met
    skill = flow_dir / "ppa_optimize" / "md" / "skill.md"
    # human gate: offers the switch to the owner flow, nothing runs
    eng = Engine(proj)
    await eng.run(yes=True)
    assert json.loads((proj.schemas_dir / "ppa_plan.json").read_text())["status"] == "met"
    offer = eng.by_name["ppa_optimize"].follow_up(eng)
    assert offer and offer["switch"] == "owner" and "action" not in offer and "format → smoke" in offer["detail"]
    assert not (proj.root / "notes" / "smoke.txt").exists()
    # auto_answer: it runs the owner's format → smoke itself, once for this RTL, and never goes back into the PPA loop
    skill.write_text(skill.read_text().replace("gate: human", "gate: auto_answer"))
    eng = Engine(proj)
    events = []
    eng.bus.subscribe(lambda e: events.append(str(e.data.get("message", ""))))
    await eng.run(yes=True)
    assert (proj.root / "notes" / "format.txt").is_file() and (proj.root / "notes" / "smoke.txt").is_file()
    assert any("running owner:format → smoke" in m for m in events) and any("owner format → smoke ended 'complete'" in m for m in events)
    assert len(fake.stages) == 1  # (no further PPA round)
    assert eng.by_name["ppa_optimize"].follow_up(eng) is None  # this RTL is verified: nothing left to offer
    n = len(events)
    await Engine(proj).run(yes=True)  # again: not run twice for the same RTL
    assert (proj.root / "notes" / "smoke.txt").read_text() == "smoke: 1 input(s)\n" and len(events) == n


class StubRunner:
    """A background run that is a process which holds run.lock and sleeps (stands in for `q3tui run`)."""

    def __new__(cls):
        from q3tui.tui.runner import Runner

        class Stub(Runner):
            def command(self, project, flow, **kw):
                code = ("import json, os, time, pathlib\n"
                        f"p = pathlib.Path({str(project.state_dir)!r}); p.mkdir(exist_ok=True)\n"
                        "(p / 'run.lock').write_text(json.dumps({'pid': os.getpid(), 'started': '2026-01-01T00:00:00'}))\n"
                        "time.sleep(60)\n")
                return [sys.executable, "-c", code]

        return Stub()


async def test_a_background_run_survives_a_flow_switch_and_stops_with_x(proj):
    import asyncio

    from q3tui.tui.app import Q3TUIApp
    from q3tui.tui.screens import ConfirmScreen

    runner = StubRunner()
    try:
        app = Q3TUIApp(proj, background=True, runner=runner)
        async with app.run_test(size=(150, 45)) as pilot:
            await pilot.pause()
            msg = app.start_run()
            assert msg.startswith("run started in the background") and runner.alive()
            assert "already going on in the background" in app.start_run()  # one run at a time
            for _ in range(50):  # the process takes the lock; the TUI notices and follows it
                await asyncio.sleep(0.2)
                if app.engine.other_run():
                    break
            assert app.engine.other_run()["pid"] == runner.pid
            app.action_switch_flow("spec")  # no confirmation, no cancel: the run is not this TUI's
            await pilot.pause()
        assert app.return_value == ("flow", "spec") and runner.alive()
        # the next TUI (the other flow) sees it, `x` stops it, quitting asks first
        app2 = Q3TUIApp(proj, background=True, runner=runner)
        async with app2.run_test(size=(150, 45)) as pilot:
            await pilot.pause()
            assert "stopping the background run" in app2.stop_any()
            for _ in range(50):
                await asyncio.sleep(0.2)
                if not runner.alive():
                    break
            assert not runner.alive()
        runner.child = None
        runner.start(proj, "fpga")
        app3 = Q3TUIApp(proj, background=True, runner=runner)
        async with app3.run_test(size=(150, 45)) as pilot:
            await pilot.pause()
            await app3.action_quit()
            await pilot.pause()
            assert isinstance(app3.screen, ConfirmScreen)
            app3.screen.dismiss(False)
            await pilot.pause()
            assert runner.alive()
    finally:
        runner.shutdown(wait=5)
    assert not runner.alive()


def test_the_background_command_carries_the_live_settings(proj):
    from q3tui.tui.runner import Runner

    proj.cfg.llm.model, proj.cfg.llm.effort = "claude-sonnet-5-5", "low"
    proj.cfg.pipeline.auto_answer = True
    cmd = Runner().command(proj, "fpga", start="sdc", stop="fpga_synth", regenerate={"sdc"})
    assert cmd[3:7] == ["-C", str(proj.root), "--flow", "fpga"] and cmd[cmd.index("--model") + 1] == "claude-sonnet-5-5"
    assert cmd[cmd.index("--effort") + 1] == "low" and cmd[cmd.index("run"):][:7] == ["run", "--from", "sdc", "--to", "fpga_synth", "--regenerate", "sdc"]
    assert "--auto-answer" in cmd


async def test_the_real_background_run_is_a_q3tui_run(proj):
    import asyncio

    from q3tui.tui.app import Q3TUIApp
    from q3tui.tui.runner import Runner

    runner = Runner()
    app = Q3TUIApp(proj, background=True, runner=runner)
    try:
        async with app.run_test(size=(150, 45)) as pilot:
            await pilot.pause()
            assert app.start_run().startswith("run started in the background")
            for _ in range(150):  # (fake Vivado: a few seconds)
                await asyncio.sleep(0.2)
                if not runner.alive():
                    break
            assert not runner.alive() and runner.child.returncode == 0
            await asyncio.sleep(2.5)  # the TUI sees the end: it re-reads the state the run wrote
            views = {v.name: v for v in app.engine.status()}  # (it stopped at the report's review gate, as `q3tui run` does)
            assert [views[n].status for n in ("sdc", "fpga_synth", "timing_util_report")] == ["done"] * 3
            assert views["timing_util_report"].gate == "open"
    finally:
        runner.shutdown(wait=5)


async def test_the_ppa_options_are_in_the_settings_dialog(proj):
    from textual.widgets import Input, TabPane

    from q3tui.core import settings as settings_mod
    from q3tui.tui.app import Q3TUIApp

    ops = Ops(Engine(proj))
    path = "pipeline.step_options.ppa_optimize.max_iterations"
    assert ops.settings()[path] == 3  # (the flow file's value)
    msg = ops.apply_settings({path: "6", "pipeline.step_options.ppa_optimize.verify_after": "off",
                              "pipeline.step_options.ppa_optimize.goal": ""}, save=True)
    assert path in msg and ops.engine.by_name["ppa_optimize"].options["max_iterations"] == 6  # the live step
    assert (proj.root / "q3tui.yaml").read_text().count("max_iterations: 6") == 1  # saved
    assert Ops(Engine(Project.open(proj.root))).settings()[path] == 6  # the next start
    with pytest.raises(EngineError, match="whole number"):
        ops.apply_settings({path: "many"})
    assert any(s.path == path for t in ops.settings_tabs() for s in t.settings) and settings_mod.is_step_option(path)
    app = Q3TUIApp(proj)
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause()
        app._select_step("ppa_optimize")
        app.action_settings()
        await pilot.pause()
        assert "set-tab-ppa_optimize" in {p.id for p in app.screen.query(TabPane)}  # the selected step's tab opens
        assert app.screen.query_one("#set-pipeline-step_options-ppa_optimize-max_iterations", Input).value == "6"


def test_the_synthesis_options_are_settings_of_fpga_synth(proj):
    ops = Ops(Engine(proj))
    base = "pipeline.step_options.fpga_synth."
    paths = {s.path for t in ops.settings_tabs() for s in t.settings}
    assert {base + k for k in ("template", "part", "package", "speed", "implement", "frontend", "synth_args")} <= paths
    ops.apply_settings({base + "implement": "on", base + "synth_args": "-flatten_hierarchy full\n-retiming"})
    step = ops.engine.by_name["fpga_synth"]
    assert step._fpga_cfg()["implement"] is True and step._fpga_cfg()["synth_args"] == ["-flatten_hierarchy full", "-retiming"]
    tcl = vivado.script_text([], "blk", proj.root / "constraints" / "sdc", proj.root, {"fpga": step._fpga_cfg()})
    assert "-flatten_hierarchy full -retiming" in tcl and "route_design" in tcl


async def test_the_fpga_is_picked_in_the_synthesis_step_view(proj):
    from textual.widgets import DataTable

    from q3tui.flows.fpga import panels
    from q3tui.tui.app import Q3TUIApp

    app = Q3TUIApp(proj)
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause()
        app._select_step("fpga_synth")
        await pilot.pause()
        rows = {r[0]: r for r in panels._synth_rows(app)}
        assert "Xilinx 7-series" in str(rows["template"][2]) and "implement" in rows and "package" not in rows  # (Vivado: no package / speed)
        app.ops.apply_settings({"pipeline.step_options.fpga_synth.template": "ecp5", "pipeline.step_options.fpga_synth.part": "45k"}, save=True)
        rows = {r[0]: r for r in panels._synth_rows(app)}
        assert rows["part"][2] == "45k" and rows["package"][2] == "CABGA381" and "speed" in rows and "frontend" in rows
        app.refresh_all()
        await pilot.pause(0.5)
        tab = app.screen.query_one(panels.SynthTab)
        assert tab.query_one(DataTable).row_count == len(rows)
        assert "45k" in panels._synth_detail(app, "part") or "`25k`" in panels._synth_detail(app, "part")


async def test_an_asic_frequency_leaves_the_fpga_results_alone(proj):
    eng = Engine(proj)
    await eng.run(yes=True, stop="timing_util_report")
    before = {v.name: v.status for v in eng.status()}
    Ops(eng).run_action("sdc_set", key="clock.i_clk.asic_freq_mhz", value="120")  # the ASIC flow's own target for the clock
    assert "i_clk: ASIC frequency 120 MHz" in Ops(eng).run_action("sdc_set", key="clock.i_clk.asic_freq_mhz", value="120")
    eng2 = Engine(Project.open(proj.root))
    await eng2.run(yes=True, stop="timing_util_report")  # (the sdc step reruns, writes no Tcl: nothing downstream goes stale)
    assert {v.name: v.status for v in eng2.status()}["fpga_synth"] == "done" and before["fpga_synth"] == "done"
    assert "asic_freq_mhz" in {k.split(".")[2] for k, _n, _v in sdc.rows(sdc.load(proj)) if k.startswith("clock.")}


# -- the ASIC library, chosen from this flow too ----------------------------------------------------------------------------------------------


def test_the_asic_library_is_a_setting_of_this_flow_too(proj):
    from q3tui.flows.fpga.lib import hints

    ops = Ops(Engine(proj))
    tabs = {t.id: t for t in ops.settings_tabs()}
    path = "pipeline.step_options.asic_synth.library"
    assert path in {s.path for s in tabs["asic_synth"].settings}  # a tab of its own: the step is the ASIC flow's
    assert ops.settings()[path].endswith("gf180mcu_fd_sc_mcu7t5v0__tt_025C_5v00.db")  # the ASIC flow file's, while nothing is set
    assert hints.asic_library(proj).endswith("tt_025C_5v00.db")
    msg = ops.apply_settings({path: "/pdk/other__ss_n40C_4v50.db"}, save=True)
    assert path in msg and "saved to q3tui.yaml" in msg
    assert ops.settings()[path] == "/pdk/other__ss_n40C_4v50.db" and hints.asic_library(proj) == "/pdk/other__ss_n40C_4v50.db"
    assert "other__ss_n40C_4v50.db" in (proj.root / "q3tui.yaml").read_text()
    assert Ops(Engine(Project.open(proj.root))).settings()[path] == "/pdk/other__ss_n40C_4v50.db"  # the next start (and the ASIC flow) read it


def test_a_reset_gets_the_input_delay_of_its_own_clock_and_extra_tcl_is_sourced_last():
    cfg = sdc.SdcConfig(clocks=[sdc.Clock(name="i_clk_a", port="i_clk_a", period_ns=10.0), sdc.Clock(name="i_clk_b", port="i_clk_b", period_ns=4.0)],
                        io=sdc.IOBudget(clock="i_clk_a"), resets=["i_rst_n_b", "i_rst_n_x"])
    ports = [{"name": n, "dir": "input", "vector": ""} for n in ("i_clk_a", "i_clk_b", "i_rst_n_b", "i_rst_n_x", "i_d")]
    io = sdc.render(cfg, ports)["io_constraints.tcl"]
    assert "set_input_delay -clock [get_clocks i_clk_b] -max 1.2 [get_ports {i_rst_n_b}]" in io  # (ends like i_clk_b: 30% of 4 ns)
    assert "set_input_delay -clock [get_clocks i_clk_a] -max 3 [get_ports {i_rst_n_x}]" in io  # (no match: the I/O clock)
    assert sdc.extra_lines("set_false_path -from [get_ports i_d]\n# a comment\nset_max_delay 3 -to [get_ports i_d]") == [
        "set_false_path -from [get_ports i_d]", "set_max_delay 3 -to [get_ports i_d]"]
    for bad in ("exec rm -rf x", "set_false_path -from [get_ports i_d]; exec ls", "set_max_delay 3 -to [get_ports {i_d]", "", "source other.tcl"):
        assert sdc.extra_lines(bad) is None
    cfg.extra_tcl = ["set_false_path -from [get_ports i_d]"]
    assert sdc.add_extra(cfg, ["set_false_path -from [get_ports i_d]", "set_max_delay 3 -to [get_ports i_d]"]) == ["set_max_delay 3 -to [get_ports i_d]"]
    assert sdc.render(cfg, ports)["extra.tcl"].endswith("set_max_delay 3 -to [get_ports i_d]\n")
