"""FPGA templates: the registry, the Yosys + nextpnr backend (script, arguments, log readers), and `fpga_synth` / `timing_util_report`
running a template with fake tools that replay recorded logs."""

import json
import sys
from pathlib import Path

import pytest

from q3tui.core.ops import Ops
from q3tui.core.project import Project
from q3tui.flows.fpga.lib import oss, templates
from q3tui.pipeline.engine import Engine, EngineError

DATA = Path(__file__).parent / "data" / "fpga_oss"
RTL = """module blk (input i_clk, input i_rst_n, input [3:0] i_a, input i_v, output reg [3:0] o_y, output o_r);
  always @(posedge i_clk or negedge i_rst_n) if (!i_rst_n) o_y <= 0; else if (i_v) o_y <= i_a;
  assign o_r = 1'b1;
endmodule
"""
FAKE_YOSYS = """import re, shutil, sys
from pathlib import Path
text = Path(sys.argv[1]).read_text()
Path(re.search(r'-json "([^"]+)"', text).group(1)).write_text('{}')
shutil.copy(Path(%r) / 'stat_ecp5.rpt', re.search(r'tee -o "([^"]+)"', text).group(1))
print('Yosys 0.58+35 (git sha1 abc)')
"""
FAKE_PNR = """import shutil, sys
from pathlib import Path
log = sys.argv[sys.argv.index('--log') + 1]
Path('args.txt').write_text(' '.join(sys.argv[1:]))
shutil.copy(Path(%r) / 'pnr_ecp5.log', log)
print('Warning: No LPF file specified')
"""


def _roles(root: Path, pnr: str = "fake_pnr.py") -> str:
    return ("tools:\n  roles:\n    fpga_yosys:\n"
            f"      cmd: \"{sys.executable} {root / 'fake_yosys.py'} {{script}}\"\n      parse: yosys\n"
            "    fpga_pnr_ecp5:\n"
            f"      cmd: \"{sys.executable} {root / pnr} {{args}}\"\n      parse: {{error: ['^ERROR:'], warning: ['^Warning:']}}\n")


@pytest.fixture
def proj(tmp_path):
    (tmp_path / "src" / "rtl").mkdir(parents=True)
    (tmp_path / "src" / "rtl" / "blk.sv").write_text(RTL)
    (tmp_path / "src" / "rtl" / "filelist.f").write_text("src/rtl/blk.sv\n")
    (tmp_path / "schemas").mkdir()
    (tmp_path / "schemas" / "structured_spec.json").write_text(json.dumps({"metadata": {"top_module": "blk"}}))
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec" / "spec.md").write_text("| **Clock** | `i_clk`, target frequency 300 MHz |\n")
    (tmp_path / "fake_yosys.py").write_text(FAKE_YOSYS % str(DATA))
    (tmp_path / "fake_pnr.py").write_text(FAKE_PNR % str(DATA))
    (tmp_path / "q3tui.yaml").write_text("pipeline:\n  flow: fpga\n  step_options:\n    fpga_synth:\n      template: ecp5\n" + _roles(tmp_path))
    return Project.open(tmp_path)


# -- the registry ---------------------------------------------------------------------------------------------------------


def test_templates():
    assert [t for t, _ in templates.choices()][:3] == ["xilinx7", "ecp5", "ice40"] and templates.get(None).id == "xilinx7"
    assert {t.id for t in templates.TEMPLATES.values() if t.backend == "quartus"} >= {"arria10", "cyclonev", "max10"}
    assert templates.get("ecp5").label == "Lattice ECP5 — Yosys + nextpnr (40 nm)" and templates.get("xilinx7").label.endswith("(28 nm)")
    with pytest.raises(ValueError, match="unknown FPGA template 'asic'"):
        templates.get("asic")


def test_the_device_of_a_template():
    ecp5, x7 = templates.get("ecp5"), templates.get("xilinx7")
    assert templates.device(ecp5, "") == ("85k", "") and templates.device(ecp5, "UM-45K") == ("um-45k", "")
    assert templates.device(x7, "xc7a100tcsg324-1") == ("xc7a100tcsg324-1", "")
    dev, note = templates.device(ecp5, "xc7k325tffg900-2")  # a part of another family left in the settings
    assert dev == "85k" and "not a ECP5 device" in note
    assert templates.device(x7, "85k")[0] == "xc7k325tffg900-2"


# -- Yosys and nextpnr -------------------------------------------------------------------------------------------------------


def test_the_yosys_script_and_the_nextpnr_arguments(tmp_path):
    t = templates.get("ecp5")
    s = oss.yosys_script([Path("/r/a.sv")], "blk", t, tmp_path / "o.json", tmp_path / "s.rpt", {"defines": {"SYNTH": 1}, "synth_args": ["-abc9"]})
    assert 'read_verilog -sv -DSYNTH=1 "/r/a.sv"' in s and f'synth_ecp5 -top blk -json "{tmp_path / "o.json"}" -abc9' in s and f'tee -o "{tmp_path / "s.rpt"}" stat' in s
    assert "read_slang --top blk" in oss.yosys_script([Path("/r/a.sv")], "blk", t, tmp_path / "o.json", tmp_path / "s.rpt", {"frontend": "slang"})
    one = [{"name": "i_clk", "port": "i_clk", "period_ns": 4.0}]
    a = oss.pnr_args(t, "85k", "CABGA381", "6", tmp_path / "o.json", tmp_path / "p.log", one, tmp_path / "c.lpf")
    assert a[:4] == ["--85k", "--package", "CABGA381", "--speed"] and "--out-of-context" in a and a[a.index("--freq") + 1] == "250" and "--lpf" not in a
    two = one + [{"name": "slow", "port": "i_slow", "period_ns": 10.0}]
    a = oss.pnr_args(t, "85k", "CABGA381", "6", tmp_path / "o.json", tmp_path / "p.log", two, tmp_path / "c.lpf")
    assert a[a.index("--lpf") + 1] == str(tmp_path / "c.lpf") and "--lpf-allow-unconstrained" in a and a[a.index("--freq") + 1] == "250"  # the fastest
    assert oss.lpf_text(two) == 'FREQUENCY PORT "i_clk" 250 MHZ;\nFREQUENCY PORT "i_slow" 100 MHZ;\n'
    ice = oss.pnr_args(templates.get("ice40"), "up5k", "sg48", "", tmp_path / "o.json", tmp_path / "p.log", one)
    assert ice[0] == "--up5k" and "--pcf-allow-unconstrained" in ice and "--out-of-context" not in ice and "--speed" not in ice


def test_nextpnr_reports():
    log = (DATA / "pnr_ecp5.log").read_text()
    u = oss.parse_utilization(log, (DATA / "stat_ecp5.rpt").read_text(), templates.get("ecp5"))
    assert u["lut"] == {"used": 8290, "available": 83640, "pct": 9.91} and u["ff"]["used"] == 1705 and u["dsp"]["used"] == 4 and u["bram_tile"]["used"] == 0
    assert oss.fmax_lines(log) == [("i_clk_core", 62.72, False, 300.0)]  # the last analysis (after routing) wins
    t = oss.parse_timing(oss.timing_report(log))
    assert t["fmax_mhz"] == 62.72 and t["met"] is False and t["wns_ns"] == pytest.approx(1000 / 300 - 1000 / 62.72, abs=1e-3)
    assert t["clocks"]["i_clk_core"]["target_mhz"] == 300.0
    paths = oss.critical_path_text(log)
    assert paths.startswith("Critical path report for clock 'i_clk_core'") and "Max frequency" not in paths
    cp = oss.critical_path(paths)
    assert cp["source"].startswith("u_vlsi_fifo_0.reg_wr_ptr") and cp["logic_levels"] >= 2 and cp["data_path_ns"] > 5
    ice = oss.parse_utilization((DATA / "pnr_ice40.log").read_text(), (DATA / "stat_ice40.rpt").read_text(), templates.get("ice40"))
    assert ice["lut"]["used"] == 36 and ice["lut"]["available"] == 7680 and ice["ff"]["used"] == 32 and ice["io"]["used"] == 35
    assert oss.fmax_lines((DATA / "pnr_ice40.log").read_text())[0][0] == "i_clk$SB_IO_IN_$glb_clk"
    assert oss.phase_line("Info: Running main analytical placer, max placement attempts per cell = 1") == "Running main analytical placer, max placement attempts per cell = 1" and oss.phase_line("Info: Packing IOs..") is None
    assert oss.phase_line("2. Executing ABC pass (technology mapping using ABC).") == "abc" and oss.phase_line("3. Executing OPT_CLEAN pass.") is None


# -- the steps ------------------------------------------------------------------------------------------------------------------------


async def test_ecp5_runs_through_the_same_steps(proj):
    eng = Engine(proj)
    await eng.run(yes=True, stop="timing_util_report")
    st = {v.name: v for v in eng.status()}
    assert st["fpga_synth"].status == "done" and st["timing_util_report"].status == "done", [(v.name, v.status, v.detail) for v in eng.status()]
    s = json.loads((proj.schemas_dir / "fpga_synth.json").read_text())
    assert s["template"] == "ecp5" and s["backend"] == "yosys" and s["part"] == "ECP5 85k CABGA381 -6" and s["tool"] == "Yosys 0.58+35 + nextpnr"
    rep = proj.root / "build" / "syn" / "fpga" / "reports"
    assert {"utilization.rpt", "timing.rpt", "paths.rpt", "pnr.rpt", "yosys_stat.rpt"} <= {f.name for f in rep.iterdir()}
    assert (proj.root / "build" / "syn" / "fpga" / "synth.ys").is_file() and not (proj.root / "build" / "syn" / "fpga" / "synth.tcl").exists()
    args = (proj.state_dir / "fpga" / "fpga_synth" / "args.txt").read_text()
    assert args.startswith("--85k --package CABGA381 --speed 6 --out-of-context") and "--freq 300" in args  # the spec's 300 MHz
    ppa = json.loads((proj.schemas_dir / "ppa_report.json").read_text())
    assert ppa["template"] == "ecp5" and ppa["utilization"]["lut"]["used"] == 8290 and ppa["timing"]["fmax_mhz"] == 62.72
    assert ppa["timing"]["wns_ns"] == pytest.approx(1000 / 300 - 1000 / 62.72, abs=1e-3) and ppa["verdict"]["timing"] == "fail" and ppa["verdict"]["overall"] == "fail"
    assert ppa["verdict"]["hold"] == "n/a" and ppa["unconstrained"] == {} and ppa["critical_path"]["source"].startswith("u_vlsi_fifo_0")


async def test_a_template_change_makes_the_synthesis_stale(proj):
    eng = Engine(proj)
    await eng.run(yes=True, stop="fpga_synth")
    ops = Ops(Engine(proj))
    ops.apply_settings({"pipeline.step_options.fpga_synth.part": "um-45k"})
    assert ops.engine.by_name["fpga_synth"].options["part"] == "um-45k"


async def test_a_missing_nextpnr_is_a_failed_step(proj):
    (proj.root / "q3tui.yaml").write_text("pipeline:\n  flow: fpga\n  step_options:\n    fpga_synth:\n      template: ecp5\n" + _roles(proj.root).replace("fake_pnr.py", "none.py").replace(
        sys.executable + f" {proj.root / 'none.py'} {{args}}", "no-such-nextpnr {args}"))
    eng = Engine(Project.open(proj.root))
    await eng.run(yes=True, stop="fpga_synth")
    st = {v.name: v for v in eng.status()}
    assert st["fpga_synth"].status == "failed" and "fpga_pnr_ecp5" in st["fpga_synth"].detail and "not run" in st["fpga_synth"].detail


async def test_an_unknown_template_is_a_failed_step(proj):
    (proj.root / "q3tui.yaml").write_text("pipeline:\n  flow: fpga\n  step_options:\n    fpga_synth:\n      template: asic\n" + _roles(proj.root))
    eng = Engine(Project.open(proj.root))
    await eng.run(yes=True, stop="fpga_synth")
    st = {v.name: v for v in eng.status()}
    assert st["fpga_port"].status == "failed" and "unknown FPGA template 'asic'" in st["fpga_port"].detail  # (the first step that reads the part says it)


# -- Settings --------------------------------------------------------------------------------------------------------------------------


def test_the_part_is_the_setting_and_its_family_follows(proj):
    from q3tui.pipeline.base import StepFailed

    ops = Ops(Engine(proj))
    paths = {s.path for s in {t.id: t for t in ops.settings_tabs()}["fpga_synth"].settings}
    base = "pipeline.step_options.fpga_synth."
    assert base + "template" not in paths and {base + "part", base + "package", base + "speed"} <= paths
    step = ops.engine.by_name["fpga_synth"]
    assert step.template().id == "ecp5" and step.part() == ("85k", "")      # (an older project's template: honoured while no part is set)
    ops.apply_settings({base + "part": "up5k"}, save=True)
    assert step.template().id == "ice40" and step.part() == ("up5k", "")     # the part wins over that template
    ops.apply_settings({base + "part": "10AX115N2F40E2LG"}, save=True)
    assert step.template().roles == ["fpga_quartus_pro"] and Engine(Project.open(proj.root)).by_name["fpga_synth"].template().family == "Arria 10"
    ops.apply_settings({base + "part": "xczu9eg-ffvb1156-2-e"})
    with pytest.raises(StepFailed, match="no FPGA family here synthesizes it"):
        step.template()


async def test_the_template_and_the_part_are_inputs_of_the_synthesis(proj):
    eng = Engine(proj)
    await eng.run(yes=True, stop="timing_util_report")
    assert {v.name: v.status for v in Engine(proj).status()}["fpga_synth"] == "done"
    (proj.root / "q3tui.yaml").write_text("pipeline:\n  flow: fpga\n  step_options:\n    fpga_synth:\n      template: ecp5\n      part: um-45k\n" + _roles(proj.root))
    st = {v.name: v.status for v in Engine(Project.open(proj.root)).status()}
    assert st["fpga_synth"] == "stale"  # another device is another result
    (proj.root / "q3tui.yaml").write_text("pipeline:\n  flow: fpga\n  step_options:\n    fpga_synth:\n      template: ecp5\n    ppa_optimize:\n      max_iterations: 9\n" + _roles(proj.root))
    st = {v.name: v.status for v in Engine(Project.open(proj.root)).status()}
    assert st["fpga_synth"] == "done" and st["timing_util_report"] == "done"  # (the round limit is not an input of the synthesis)
