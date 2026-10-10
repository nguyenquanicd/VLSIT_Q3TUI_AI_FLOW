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
    assert "i_clk" not in io.replace("[get_clocks i_clk]", "") and "i_rst_n" not in io  # clock and reset get no I/O delay
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
    assert "create_clock -name i_clk -period 5 [get_ports i_clk_core]" in (proj.root / "sdc" / "clocks.tcl").read_text()


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


def test_the_fpga_role_falls_back_to_the_flows_default_only_for_that_flow(tmp_path):
    from q3tui.core.config import Config
    from q3tui.eda.tools import load_tools

    plain = load_tools(Config(), tmp_path)
    assert "fpga_synth" not in plain.roles and "lint" in plain.roles  # a project's tool set is exactly its own
    ts = load_tools(Config(), tmp_path, flows.load_flow("fpga"))
    assert ts.role("fpga_synth").modules == ["xilinx/vivado"] and "lint" in ts.roles  # (the Synopsys preset stays)
    assert "fpga_synth" not in load_tools(Config(), tmp_path, flows.load_flow("vlsit")).roles


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
    assert (proj.root / "sdc" / "io_constraints.tcl").is_file() and (proj.root / "fpga" / "synth.tcl").is_file()
    assert 'read_xdc -mode out_of_context' in (proj.root / "fpga" / "synth.tcl").read_text()
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
    left = subprocess.run(["pgrep", "-f", str(proj.root / "fpga" / "synth.tcl")], capture_output=True, text=True).stdout.split()
    assert left == [], "the tool was left running after the stop"


async def test_a_report_without_timing_says_why(proj):
    eng = Engine(proj)
    await eng.run(yes=True)
    rpt = proj.root / "fpga" / "reports" / "timing.rpt"
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
    assert "2.5 ns" in msg and "create_clock -name i_clk -period 2.5" in (proj.root / "sdc" / "clocks.tcl").read_text()
    st = {v.name: v.status for v in Engine(proj).status()}
    assert st["sdc"] == "stale" and st["fpga_synth"] == "stale"
    with pytest.raises(EngineError, match="positive"):
        ops.run_action("sdc_set", key="clock.i_clk.freq_mhz", value="-5")
    assert "400" in json.dumps(json.loads((proj.root / "sdc" / "sdc.json").read_text()) and _rows(ops))
    ops.run_action("sdc_add", what="exception", text="false_path from=i_v to=o_y")
    assert "set_false_path -from [get_ports i_v] -to [get_ports o_y]" in (proj.root / "sdc" / "exceptions.tcl").read_text()
    ops.run_action("sdc_remove", key="exception.0")
    assert "i_v" not in (proj.root / "sdc" / "exceptions.tcl").read_text().replace("i_vld", "")
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
        assert sections["clocks"].rows(app) == [("clock.i_clk", "i_clk", "i_clk", "200", "5", "0", "50")]
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
        assert ed.spec.id == "clocks" and len(items.columns) == 6

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
        assert sections["clocks"].rows(app)[1][1:6] == ("clk2", "i_clk", "250", "4", "0.2") and sections["clocks"].rows(app)[0][3] == "200"
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
          ppa_optimize=("handoff_flow: vlsit", "handoff_flow: owner"))
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


def test_settings_follow_the_flows_steps(proj):
    ops = Ops(Engine(proj))
    tabs = {t.id: t for t in ops.settings_tabs()}
    assert not {"spec", "rtl", "tb"} & set(tabs)  # the vlsit tabs are not this flow's
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
        assert "vlsit" in app._completions("flow") and "fpga" in app._completions("flow")
        app.action_switch_flow("fpga")  # already there: nothing happens
        await pilot.pause()
        assert app.return_value is None
        app.action_switch_flow("nope")  # unknown: stays, says why
        await pilot.pause()
        assert app.return_value is None and "flow: fpga" in (proj.root / "q3tui.yaml").read_text()
        app.action_switch_flow("vlsit")
        await pilot.pause()
    assert app.return_value == ("flow", "vlsit")
    assert load_config(None, cwd=proj.root).pipeline.flow == "vlsit"


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
        assert {t for t, g in by_title.items() if g.developed} == {"Spec", "RTL gen", "FPGA synthesis"}
        assert by_title["DV"].has_class("off") and by_title["FPGA synthesis"].has_class("current")
        app.screen.pick(by_title["DV"])  # not developed: stays open
        await pilot.pause()
        assert isinstance(app.screen, FlowMapScreen)
        app.screen.pick(by_title["Spec"])  # a box of the vlsit flow
        await pilot.pause()
    assert app.return_value == ("flow", "vlsit")
