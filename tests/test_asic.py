"""The ASIC flow: the port check, the Design Compiler script and report readers, Formality, the four steps with fake tools,
the library setting, the hand-off of the port findings, the flow map and the panels."""

import json
import sys
from pathlib import Path

import pytest

from q3tui.tui.app import Q3TUIApp

from q3tui import flows
from q3tui.core.ops import Ops
from q3tui.core.project import Project
from q3tui.flows.asic.lib import dc, fm, port
from q3tui.flows.fpga.lib import sdc
from q3tui.pipeline.engine import Engine, EngineError

DATA = Path(__file__).parent / "data" / "asic"
RTL = """`timescale 1ns/1ps
module blk (input i_clk, input i_rst_n, input [3:0] i_a, input i_v, output reg [3:0] o_y, output o_r);
  always @(posedge i_clk or negedge i_rst_n) if (!i_rst_n) o_y <= 0; else if (i_v) o_y <= i_a;
  assign o_r = 1'b1;
endmodule
"""
DIRTY = """module mem_wrap #(parameter DEPTH = 64) (input i_clk, input [7:0] i_d, output reg [7:0] o_q);
  (* ram_style = "block" *) logic [7:0] r_mem [DEPTH];
  logic [7:0] r_small [4];
  logic r_ok = 1'b0;
  initial begin $readmemh("init.hex", r_mem); end
  BUFG u_buf (.I(i_clk), .O(w_clk));
  assign o_bus = r_ok ? i_d : 8'bz;
  // synopsys translate_off
  initial $display("sim only");
  // synopsys translate_on
  always @(posedge i_clk) o_q <= r_mem[0];
endmodule
"""
FAKE_DC = """import re, shutil, sys
from pathlib import Path
text = Path(sys.argv[1]).read_text()
work, data = Path(sys.argv[2]), Path(%r)
for n in ('area', 'timing', 'qor', 'power', 'constraints', 'check_design', 'check_timing'):
    shutil.copy(data / (n + '.rpt'), work / (n + '.rpt'))
net = Path(re.search(r'write -format verilog -hierarchy -output "([^"]+)"', text).group(1))
net.parent.mkdir(parents=True, exist_ok=True)
net.write_text('module blk(); endmodule\\n')
svf = re.search(r'set_svf "([^"]+)"', text)
Path(svf.group(1)).write_text('guidance\\n')
print('Design Compiler Version: V-2023.12-SP3')
print('Warning: something minor. (OPT-1006)')
"""
FAKE_FM = """import sys
from pathlib import Path
Path(sys.argv[2], 'lec.rpt').write_text('matched points\\n')
print('Verification %s')
print('  %s Passing compare points')
"""


class PorterLLM:
    """The asic_port porter (an LLM stage): answers with `plan` (a PortPlan) and keeps the stages it was asked."""

    def __init__(self, plan=None):
        from q3tui.flows.asic.lib.porter import PortPlan

        self.plan, self.stages = plan or PortPlan(), []

    async def __call__(self, stage, cfg, emit):
        from q3tui.llm.runtime import StageResult

        self.stages.append(stage)
        emit.add_cost(0.01)
        return StageResult("ok", self.plan, 0.01, 2, "s")


@pytest.fixture(autouse=True)
def llm(monkeypatch):
    from q3tui.llm import runtime

    fake = PorterLLM()
    monkeypatch.setattr(runtime, "run_stage", fake)  # (no live LLM: the porter finds nothing unless a test says so)
    return fake


@pytest.fixture
def proj(tmp_path):
    (tmp_path / "src" / "rtl").mkdir(parents=True)
    (tmp_path / "src" / "rtl" / "blk.sv").write_text(RTL)
    (tmp_path / "src" / "rtl" / "filelist.f").write_text("src/rtl/blk.sv\n")
    (tmp_path / "schemas").mkdir()
    (tmp_path / "schemas" / "structured_spec.json").write_text(json.dumps({"metadata": {"top_module": "blk"}}))
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec" / "spec.md").write_text("| **Clock** | `i_clk`, target frequency 200 MHz (5 ns period) |\n")
    (tmp_path / "lib.db").write_text("fake library\n")
    (tmp_path / "fake_dc.py").write_text(FAKE_DC % str(DATA))
    (tmp_path / "fake_fm.py").write_text(FAKE_FM % ("SUCCEEDED", "12"))
    _yaml(tmp_path)
    return Project.open(tmp_path)


def _yaml(root: Path, fm_py: str = "fake_fm.py", extra: str = "") -> None:
    (root / "q3tui.yaml").write_text(
        "pipeline:\n  flow: asic\n  step_options:\n    asic_synth:\n      library: " + str(root / "lib.db") + "\n" + extra
        + "tools:\n  roles:\n    asic_synth:\n"
        f"      cmd: \"{sys.executable} {root / 'fake_dc.py'} {{script}} {{workdir}}\"\n      parse: dc\n"
        f"    asic_lec:\n      cmd: \"{sys.executable} {root / fm_py} {{script}} {{workdir}}\"\n      parse: generic\n")


# -- the flow ---------------------------------------------------------------------------------------------------------


def test_flow_loads():
    flow = flows.load_flow("asic", Path.cwd())
    assert [s.id for s in flow.steps] == ["asic_port", "sdc", "asic_synth", "asic_lec"]
    assert flow.gates == {"asic_port": "human", "asic_synth": "human", "asic_lec": "human"}
    assert {"asic_handoff", "sdc_set", "sdc_show"} <= set(flow.actions)
    assert {s.path for s in flow.settings} >= {"pipeline.step_options.asic_synth.library", "pipeline.step_options.asic_synth.min_slack_ns"}


def test_the_default_library_is_the_gf180_cell_library():
    synth = flows.load_flow("asic", Path.cwd()).spec("asic_synth")
    assert synth.options["library"].endswith("gf180mcu_fd_sc_mcu7t5v0__tt_025C_5v00.db") and synth.options["compile"] == "compile_ultra"


# -- the port check ------------------------------------------------------------------------------------------------------


def test_the_port_check_finds_what_an_asic_cannot_take(tmp_path):
    f = tmp_path / "mem_wrap.sv"
    f.write_text(DIRTY)
    found = port.scan([f], tmp_path)
    rules = [x["rule"] for x in found]
    assert rules.count("fpga_attribute") == 1 and "vendor_primitive" in rules and "mem_init" in rules and "tristate" in rules
    assert rules.count("memory_array") == 1  # the 64-word memory; the 4-word one is below the threshold
    assert rules.count("initial_value") == 2  # the initialiser and the initial block
    assert "sim_only" not in rules  # inside translate_off
    mem = next(x for x in found if x["rule"] == "memory_array")
    assert mem["module"] == "mem_wrap" and mem["line"] == 2 and "64 words" in mem["text"] and mem["severity"] == "warn"
    assert next(x for x in found if x["rule"] == "vendor_primitive")["severity"] == "error"
    assert [x["id"] for x in found] == [f"P{i}" for i in range(1, len(found) + 1)]
    assert not port.scan([f], tmp_path, waive=("memory_array", "mem_init"))[0]["rule"] in ("memory_array", "mem_init")
    assert not any(x["rule"] == "memory_array" for x in port.scan([f], tmp_path, min_words=100))


def test_clean_rtl_has_no_finding(tmp_path):
    f = tmp_path / "blk.sv"
    f.write_text(RTL)
    assert port.scan([f], tmp_path) == []  # (`timescale is plain RTL)


def test_a_finding_becomes_a_scoped_change_request():
    f = {"rule": "memory_array", "module": "mem_wrap", "file": "src/rtl/mem_wrap.sv", "line": 2, "text": "logic [7:0] r_mem [64];", "fix": port.RULES["memory_array"].fix}
    t = port.handoff_text(f)
    assert t.startswith("[rtl:mem_wrap] ASIC port (src/rtl/mem_wrap.sv:2, memory array without a macro)") and "SRAM" in t


# -- Design Compiler -------------------------------------------------------------------------------------------------------


def test_the_dc_script(tmp_path):
    t = dc.script_text([Path("/r/a.sv")], "blk", ["/l/x.db"], Path("/p/sdc/sdc.tcl"), tmp_path, tmp_path / "net" / "blk.v", {"extra_tcl": ["set_max_area 0"], "max_paths": 7})
    assert t.index("set_svf") < t.index("analyze")  # the guidance is recorded from the start
    assert 'set_app_var target_library [list "/l/x.db"]' in t and 'set_app_var link_library [list "*" "/l/x.db"]' in t
    assert t.index("link") < t.index("set_max_area 0") < t.index('source -echo "/p/sdc/sdc.tcl"') < t.index("compile_ultra")
    assert "report_timing -max_paths 7" in t and f'write -format verilog -hierarchy -output "{tmp_path / "net" / "blk.v"}"' in t and t.rstrip().endswith("exit")


def test_dc_reports():
    a = dc.parse_area((DATA / "area.rpt").read_text())
    assert a["total_um2"] == 228625.692253 and a["cells"] == 7067 and a["sequential_cells"] == 1687 and a["sequential_um2"] == 126687.191132
    t = dc.parse_timing((DATA / "timing.rpt").read_text())
    assert t["slack_ns"] == -0.61 and t["met"] is False and t["startpoint"].startswith("u_vlsi_axfsm_1_reg_axsize")
    q = dc.parse_qor((DATA / "qor.rpt").read_text())
    assert q["wns_ns"] == -0.61 and q["tns_ns"] == -325.55 and q["violating_paths"] == 1040 and q["logic_levels"] == 12
    g = dc.parse_groups((DATA / "qor.rpt").read_text())
    assert g == [{"group": "i_clk", "slack_ns": -0.61, "period_ns": 3.33, "target_mhz": 300.3, "fmax_mhz": 253.8, "bound": "estimate"}]  # 1000 / (3.33 + 0.61)
    assert dc.parse_groups((DATA / "qor.rpt").read_text(), {"i_clk": 3.3333})[0]["fmax_mhz"] == 253.6 and dc.parse_groups("Timing Path Group '**async_default**'\n Critical Path Slack: 1.0") == []
    met = dc.parse_groups("Timing Path Group 'clk'\n Critical Path Slack: 0.00\n Critical Path Clk Period: 4.17\n")
    assert met[0]["bound"] == "at_least" and met[0]["fmax_mhz"] == 239.8  # met: the tool stops at zero slack, the clock can go faster
    assert dc.violations("  o_bid[0]   0.00  0.00  0.00  (VIOLATED: increase significant digits)\n  x/D   2.98 3.58 r  -0.61  (VIOLATED)\n") == ["x/D   2.98 3.58 r  -0.61  (VIOLATED)"]
    p = dc.parse_power((DATA / "power.rpt").read_text())
    assert p["dynamic_uw"] == 313943.6 and p["leakage_uw"] == 1.4572
    assert len(dc.violations((DATA / "constraints.rpt").read_text())) >= 2
    assert dc.check_design((DATA / "check_design.rpt").read_text()) == {"Warning": 4}
    assert dc.phase_line("  Beginning Mapping Optimizations  (Medium effort)") and dc.phase_line("  0:01:08  230127.2  0.72") is None


# -- Formality -----------------------------------------------------------------------------------------------------------------


def test_the_formality_script(tmp_path):
    svf = tmp_path / "blk.svf"
    svf.write_text("x")
    t = fm.script_text([Path("/r/a.sv")], "blk", ["/l/x.db"], Path("/n/blk.v"), svf, tmp_path)
    assert t.index("set_svf") < t.index("read_sverilog") < t.index("set_top r:/WORK/blk") < t.index("read_verilog -container i -netlist") < t.index("match") < t.index("verify")
    assert "set_top i:/WORK/blk" in t and "Q3TUI_LEC_RESULT" in t


def test_formality_results():
    real = fm.parse_result((DATA / "fm_pass.log").read_text())  # a recorded run
    assert real == {"status": "equivalent", "passing": 1807, "unmatched": 1, "rtl_messages": True}
    assert fm.parse_result("...\nVerification SUCCEEDED\n 120 Passing compare points\n")["status"] == "equivalent"
    assert fm.parse_result("Error: x\nLinking requires the following command to be executed:\nset_mismatch_message_filter -warn FMR_ELAB-147\n")["unsuppressed"] == ["FMR_ELAB-147"]
    assert "unsuppressed" not in fm.parse_result("set_mismatch_message_filter -warn FMR_ELAB-147\nVerification SUCCEEDED\n")  # (the script's own echo)
    r = fm.parse_result("Verification FAILED\n 3 Failing compare points\n")
    assert r["status"] == "not_equivalent" and r["failing"] == 3
    assert fm.parse_result("Verification INCONCLUSIVE")["status"] == "inconclusive" and fm.parse_result("crashed")["status"] == "unknown"


# -- the steps -------------------------------------------------------------------------------------------------------------------


async def test_the_steps_run(proj):
    eng = Engine(proj)
    await eng.run(yes=True)
    assert all(v.status == "done" for v in eng.status()), [(v.name, v.status, v.detail) for v in eng.status()]
    p = json.loads((proj.schemas_dir / "asic_port.json").read_text())
    assert p["status"] == "pass" and p["findings"] == []
    s = json.loads((proj.schemas_dir / "asic_synth.json").read_text())
    assert s["tool"] == "Design Compiler V-2023.12-SP3" and s["area"]["total_um2"] == 228625.692253 and s["timing"]["slack_ns"] == -0.61
    assert s["timing"]["fmax_mhz"] == 178.3 and s["timing"]["groups"][0]["target_mhz"] == 200.0  # the SDC's 5 ns clock with the recorded -0.61 ns
    assert s["verdict"]["timing"] == "fail" and s["verdict"]["overall"] == "fail" and s["violation_count"] >= 2 and s["svf"].endswith("blk.svf")
    assert (proj.root / "asic" / "netlist" / "blk.v").is_file() and (proj.root / "asic" / "netlist" / "blk.svf").is_file()
    assert (proj.root / "asic" / "reports" / "area.rpt").is_file() and 'source -echo' in (proj.root / "asic" / "synth.tcl").read_text()
    lec = json.loads((proj.schemas_dir / "asic_lec.json").read_text())
    assert lec["status"] == "equivalent" and lec["passing"] == 12
    assert (proj.root / "sdc" / "sdc.tcl").is_file()  # the shared constraints


async def test_a_clock_change_makes_the_synthesis_stale(proj):
    eng = Engine(proj)
    await eng.run(yes=True)
    Ops(eng).run_action("sdc_set", key="clock.i_clk.freq_mhz", value="100")
    st = {v.name: v.status for v in Engine(proj).status()}
    assert st["asic_synth"] == "stale" and st["asic_port"] == "done"


async def test_no_library_is_a_failed_step_not_a_pass(proj):
    (proj.root / "lib.db").unlink()
    eng = Engine(Project.open(proj.root))
    await eng.run(yes=True)
    st = {v.name: v for v in eng.status()}
    assert st["asic_synth"].status == "failed" and "library not found" in st["asic_synth"].detail
    assert st["asic_lec"].status != "done"


async def test_a_netlist_that_differs_fails_the_lec(proj):
    (proj.root / "fake_fm.py").write_text(FAKE_FM % ("FAILED", "9"))
    eng = Engine(proj)
    await eng.run(yes=True)
    st = {v.name: v for v in eng.status()}
    assert st["asic_synth"].status == "done" and st["asic_lec"].status == "failed" and "not equivalent" in st["asic_lec"].detail


async def test_the_port_findings_are_shown_and_waivable(proj):
    (proj.root / "src" / "rtl" / "mem_wrap.sv").write_text(DIRTY)
    (proj.root / "src" / "rtl" / "filelist.f").write_text("src/rtl/blk.sv\nsrc/rtl/mem_wrap.sv\n")
    eng = Engine(proj)
    await eng.run(yes=True, only="asic_port")
    p = json.loads((proj.schemas_dir / "asic_port.json").read_text())
    assert p["status"] == "fail" and p["errors"] >= 1 and p["warnings"] >= 3 and {f["module"] for f in p["findings"]} == {"mem_wrap"}


async def test_handoff_sends_the_findings_to_the_owning_flow(proj):
    (proj.root / "flows").mkdir(exist_ok=True)
    (proj.root / "flows" / "owner.yaml").write_text("name: owner\nsteps:\n  - {id: spec, kind: spec}\n  - {id: rtl, kind: reads, deps: [spec]}\n")
    (proj.root / "src" / "rtl" / "mem_wrap.sv").write_text(DIRTY)
    (proj.root / "src" / "rtl" / "filelist.f").write_text("src/rtl/blk.sv\nsrc/rtl/mem_wrap.sv\n")
    _yaml(proj.root, extra="    asic_port:\n      handoff_flow: owner\n")
    eng = Engine(Project.open(proj.root))
    await eng.run(yes=True, only="asic_port")
    ops = Ops(eng)
    msg = ops.run_action("asic_handoff", ids="P1")
    assert "P1 → owner:rtl" in msg and "q3tui --flow owner run" in msg
    other = Engine(proj, flow=flows.load_flow("owner", proj.root))
    (req,) = other.state.feedback["rtl"]
    assert req.startswith("[rtl:mem_wrap] ASIC port (src/rtl/mem_wrap.sv:")
    ops.run_action("asic_handoff")  # the rest
    with pytest.raises(EngineError, match="no finding"):
        ops.run_action("asic_handoff")
    p = json.loads((proj.schemas_dir / "asic_port.json").read_text())
    assert all(f["handed_off"]["flow"] == "owner" for f in p["findings"])
    assert {v.name: v for v in Engine(Project.open(proj.root)).status()}["asic_port"].edited == []  # recording the hand-off is not a user edit


# -- the porter (LLM) ----------------------------------------------------------------------------------------------------------

MEM_RTL = """module m_fifo (input i_clk, input i_we, input [4:0] i_a, input [7:0] i_d, output reg [7:0] o_q);
  logic [7:0] r_w0, r_w1;
  always @(posedge i_clk) if (i_we) r_w0 <= i_d;
endmodule
"""


def _plan(*changes):
    from q3tui.flows.asic.lib.porter import PortChange, PortPlan

    return PortPlan(changes=[PortChange(rule=r, module=m, line=n, text="code", wrapper=f"m_{m}_sram", fix="make a wrapper") for r, m, n in changes], summary="ok")


def _two_modules(proj):
    (proj.root / "src" / "rtl" / "m_fifo.sv").write_text(MEM_RTL)
    (proj.root / "src" / "rtl" / "filelist.f").write_text("src/rtl/blk.sv\nsrc/rtl/m_fifo.sv\n")


def test_the_rule_file_lists_the_port_rules():
    from q3tui.flows.asic.lib import porter

    text = (Path(port.__file__).parents[1] / "asic_port" / "rules" / "asic_port_rules.md").read_text()
    rules = porter.port_rules(text)
    assert {"mem_sram_wrapper", "sync_lib_cell", "clock_gate_icg"} <= set(rules) and all(r["severity"] in ("error", "warn") for r in rules.values())
    assert not set(rules) & set(port.RULES)  # (the scan's rules are not the porter's)


async def test_the_porter_adds_what_the_scan_cannot_see(proj, llm):
    _two_modules(proj)
    llm.plan = _plan(("mem_sram_wrapper", "m_fifo", 2), ("nope", "m_fifo", 2), ("sync_lib_cell", "ghost", 1))
    eng = Engine(proj)
    await eng.run(yes=True, only="asic_port")
    (stage,) = llm.stages
    assert stage.name == "asic_port" and set(stage.builtin_tools) == {"Read", "Grep", "Glob"} and "src/rtl/m_fifo.sv" in stage.prompt
    assert "## asic_port_rules.md" in stage.prompt and "mem_sram_wrapper" in stage.prompt and stage.output_model.__name__ == "PortPlan"
    p = json.loads((proj.schemas_dir / "asic_port.json").read_text())
    (f,) = p["findings"]
    assert f["rule"] == "mem_sram_wrapper" and f["source"] == "llm" and f["wrapper"] == "m_m_fifo_sram" and f["module"] == "m_fifo" and f["file"] == "src/rtl/m_fifo.sv" and f["id"] == "P1"
    assert p["status"] == "warn" and len(p["dropped"]) == 2  # an unknown rule and an unknown module are dropped, said


async def test_only_changed_files_are_ported_again(proj, llm):
    _two_modules(proj)
    llm.plan = _plan(("mem_sram_wrapper", "m_fifo", 2))
    eng = Engine(proj)
    await eng.run(yes=True, only="asic_port")
    assert len(llm.stages) == 1
    (proj.root / "src" / "rtl" / "blk.sv").write_text(RTL + "// changed\n")  # one file changes: only it goes back to the porter
    eng = Engine(Project.open(proj.root))
    await eng.run(yes=True, only="asic_port")
    assert len(llm.stages) == 2 and "src/rtl/blk.sv" in llm.stages[1].prompt and "src/rtl/m_fifo.sv" not in llm.stages[1].prompt.split("# This run")[1].split("All modules")[0]
    p = json.loads((proj.schemas_dir / "asic_port.json").read_text())
    assert [f["module"] for f in p["findings"]] == ["m_fifo"]  # (kept from the first run)


async def test_a_code_finding_is_not_ported_twice_and_the_porter_can_be_off(proj, llm):
    (proj.root / "src" / "rtl" / "mem_wrap.sv").write_text(DIRTY)
    (proj.root / "src" / "rtl" / "filelist.f").write_text("src/rtl/blk.sv\nsrc/rtl/mem_wrap.sv\n")
    llm.plan = _plan(("mem_sram_wrapper", "mem_wrap", 2))  # the scan has a memory_array finding at that line
    eng = Engine(proj)
    await eng.run(yes=True, only="asic_port")
    p = json.loads((proj.schemas_dir / "asic_port.json").read_text())
    assert not [f for f in p["findings"] if f["source"] == "llm"] and any(f["source"] == "code" for f in p["findings"])
    _yaml(proj.root, extra="    asic_port:\n      port: false\n")
    llm.stages.clear()
    eng = Engine(Project.open(proj.root))
    await eng.run(yes=True, only="asic_port")
    assert llm.stages == []


async def test_a_port_order_is_handed_off_with_its_wrapper(proj, llm):
    (proj.root / "flows").mkdir(exist_ok=True)
    (proj.root / "flows" / "owner.yaml").write_text("name: owner\nsteps:\n  - {id: spec, kind: spec}\n  - {id: rtl, kind: reads, deps: [spec]}\n")
    _two_modules(proj)
    llm.plan = _plan(("mem_sram_wrapper", "m_fifo", 2))
    _yaml(proj.root, extra="    asic_port:\n      handoff_flow: owner\n")
    eng = Engine(Project.open(proj.root))
    await eng.run(yes=True, only="asic_port")
    Ops(eng).run_action("asic_handoff")
    (req,) = Engine(proj, flow=flows.load_flow("owner", proj.root)).state.feedback["rtl"]
    assert req.startswith("[rtl:m_fifo] ASIC port (src/rtl/m_fifo.sv:2, ") and "make a wrapper" in req and "Create the wrapper module `m_m_fifo_sram`" in req


# -- Settings and the TUI -----------------------------------------------------------------------------------------------------------


def test_the_library_is_a_setting(proj):
    ops = Ops(Engine(proj))
    tabs = {t.id: t for t in ops.settings_tabs()}
    assert {"asic_synth", "asic_port"} <= set(tabs) and "sdc" not in {s.path.split(".")[2] for t in tabs.values() for s in t.settings if s.path.startswith("pipeline.step")}
    lib = "pipeline.step_options.asic_synth.library"
    assert ops.settings()[lib].endswith("lib.db") and ops.settings()["pipeline.step_options.asic_synth.compile"] == "compile_ultra"
    ops.apply_settings({lib: "/other/lib.db", "pipeline.step_options.asic_synth.max_area_um2": "5000"}, save=True)
    assert ops.engine.by_name["asic_synth"].options["library"] == "/other/lib.db" and ops.engine.by_name["asic_synth"].options["max_area_um2"] == 5000
    assert Ops(Engine(Project.open(proj.root))).settings()[lib] == "/other/lib.db"


async def test_the_flow_map_has_the_asic_box(proj):
    from q3tui.tui.flowmap import FlowMapScreen, Group

    app = Q3TUIApp(proj)
    async with app.run_test(size=(150, 70)) as pilot:
        await pilot.pause()
        app.action_switch_flow()
        await pilot.pause()
        assert isinstance(app.screen, FlowMapScreen)
        groups = {str(g.border_title).split("  ·")[0]: g for g in app.screen.query(Group)}
        assert groups["ASIC synthesis"].developed and groups["ASIC synthesis"].has_class("current")
        app.screen.pick(groups["FPGA synthesis"])
        await pilot.pause()
    assert app.return_value == ("flow", "fpga")


async def test_the_panels(proj):
    eng = Engine(proj)
    await eng.run(yes=True)
    app = Q3TUIApp(proj)
    async with app.run_test(size=(160, 50)) as pilot:
        await pilot.pause()
        from q3tui.flows.asic import panels

        assert [t.id for t in panels._tabs_for("asic.asic_synth")] == ["qor"] and [t.id for t in panels._tabs_for("asic.sdc")] == ["sdc"]
        rows = {r[0]: r for r in panels._synth_rows(app)}
        assert rows["slack"][1] == "Worst setup slack (ns)" and rows["area"][2] == "228625.692253"
        assert [r[0] for r in panels._lec_rows(app)][0] == "status"


# -- the clock of the ASIC, and what the FPGA flow's SDC says about it ---------------------------------------------------------------


async def test_the_asic_has_its_own_clock(proj):
    eng = Engine(proj)
    await eng.run(yes=True, stop="sdc")
    Ops(eng).run_action("sdc_set", key="clock.i_clk.asic_freq_mhz", value="100")
    assert "-period 5 " in (proj.root / "sdc" / "clocks.tcl").read_text()  # the shared SDC (the FPGA's) is as it was: 200 MHz
    eng = Engine(Project.open(proj.root))
    await eng.run(yes=True, stop="asic_synth")
    assert "-period 10 " in (proj.root / "asic" / "sdc" / "clocks.tcl").read_text() and "-period 5 " in (proj.root / "sdc" / "clocks.tcl").read_text()
    assert 'source -echo "' + str(proj.root / "asic" / "sdc" / "sdc.tcl") in (proj.root / "asic" / "synth.tcl").read_text()
    s = json.loads((proj.schemas_dir / "asic_synth.json").read_text())
    assert s["clock_override"] == {"i_clk": 100.0} and s["clocks"][0]["period_ns"] == 10.0 and s["timing"]["groups"][0]["target_mhz"] == 100.0
    assert s["timing"]["fmax_mhz"] == pytest.approx(1000 / (10.0 + 0.61), abs=0.1)  # the recorded -0.61 ns slack against a 10 ns period
    Ops(eng).run_action("sdc_set", key="clock.i_clk.asic_freq_mhz", value="")  # empty: the same as the FPGA's again
    assert sdc.load(proj).clocks[0].asic_freq_mhz is None


async def test_only_the_asic_synthesis_goes_stale_on_an_asic_clock(proj):

    eng = Engine(proj)
    await eng.run(yes=True, stop="asic_synth")
    Ops(eng).run_action("sdc_set", key="clock.i_clk.asic_freq_mhz", value="150")
    st = {v.name: v.status for v in Engine(Project.open(proj.root)).status()}
    assert st["asic_synth"] == "stale" and st["asic_port"] == "done"  # (the sdc step reruns, writes nothing and keeps the shared Tcl files: the FPGA flow is not made stale)
    assert sdc.load(proj).clocks[0].asic_freq_mhz == 150.0
    with pytest.raises(ValueError, match="positive"):
        sdc.apply_edit(sdc.load(proj), "clock.i_clk.asic_freq_mhz", "0")


async def test_the_sdc_tab_and_the_log_tell_the_asic_fmax(proj):
    from q3tui.flows.fpga.lib import hints

    assert hints.asic_hint(proj) == {} and hints.asic_note(proj, "i_clk") == ""  # not run yet
    eng = Engine(proj)
    await eng.run(yes=True, stop="asic_synth")
    h = hints.asic_hint(proj)["i_clk"]
    assert h["fmax_mhz"] == 178.3 and h["target_mhz"] == 200.0 and h["library"] == "lib" or h["fmax_mhz"] == 178.3  # (the fake library is lib.db)
    assert hints.asic_note(proj, "i_clk").startswith("ASIC ≈ 178.3 MHz")
    ops = Ops(Engine(proj))
    assert "i_clk: ASIC ≈ 178.3 MHz" in ops.run_action("sdc_set", key="clock.i_clk.freq_mhz", value="250")  # typing a frequency tells the expected Fmax
    (proj.root / "src" / "rtl" / "blk.sv").write_text(RTL + "// changed\n")
    assert "RTL changed since" in hints.asic_note(proj, "i_clk") and hints.asic_hint(proj)["i_clk"]["stale"]
    from q3tui.flows.fpga import panels as fp

    app = Q3TUIApp(proj)
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause()
        (row,) = fp.SECTIONS[0].rows(app)
        assert str(row[4]) == "same" and str(row[8]) == "178.3 *"  # no ASIC frequency of its own; * : the RTL changed since that run


# -- the quick estimate ----------------------------------------------------------------------------------------------------------------------


def test_the_probe_constraints(proj):
    from q3tui.flows.asic.lib import estimate

    cfg = sdc.default_config([{"name": "i_clk", "dir": "input", "vector": False}], "clock frequency 200 MHz")
    cfg.clocks[0].uncertainty_ns = 0.1
    t = estimate.probe_tcl(cfg)
    assert "create_clock -name i_clk -period 1 [get_ports i_clk]" in t and "set_clock_uncertainty 0.1 [get_clocks i_clk]" in t
    assert "input_delay" not in t and "output_delay" not in t  # (register to register: the I/O budget scales with the period)


async def test_the_quick_estimate(proj):
    from q3tui.flows.fpga.lib import hints

    ops = Ops(Engine(proj))
    await ops.engine.run(yes=True, stop="sdc")
    msg = ops.run_action("asic_estimate")
    assert msg.startswith("ASIC estimate (quick,") and "i_clk ≈ 621.1 MHz" in msg  # the recorded -0.61 ns slack against the 1 ns probe
    d = json.loads((proj.schemas_dir / "asic_estimate.json").read_text())
    assert d["mode"] == "quick" and d["probe_ns"] == 1.0 and d["timing"]["groups"][0]["fmax_mhz"] == 621.1 and d["rtl_sha"] == hints.rtl_sha(proj)
    script = (proj.state_dir / "asic" / "estimate" / "estimate.tcl").read_text()
    assert "compile -map_effort low" in script and "compile_ultra" not in script and "probe.tcl" in script
    assert hints.asic_hint(proj)["i_clk"]["source"] == "estimate" and hints.asic_note(proj, "i_clk") == "ASIC ≈ 621.1 MHz (quick estimate, lib)"
    assert not (proj.root / "asic" / "netlist").exists()  # (an estimate leaves no deliverable behind)


async def test_a_fresh_synthesis_beats_an_estimate_and_a_stale_one_does_not(proj):
    from q3tui.flows.fpga.lib import hints

    eng = Engine(proj)
    await eng.run(yes=True, stop="asic_synth")
    Ops(eng).run_action("asic_estimate")
    assert hints.asic_hint(proj)["i_clk"]["source"] == "synthesis"  # exact, and fresh
    (proj.root / "src" / "rtl" / "blk.sv").write_text(RTL + "// changed\n")
    assert hints.asic_hint(proj)["i_clk"]["stale"] and hints.asic_hint(proj)["i_clk"]["source"] == "synthesis"  # nothing fresher: the old numbers, marked
    Ops(Engine(Project.open(proj.root))).run_action("asic_estimate")
    h = hints.asic_hint(proj)["i_clk"]
    assert h["source"] == "estimate" and not h["stale"]  # the estimate of the new RTL beats the full result of the old one


async def test_the_estimate_needs_what_it_needs(proj):
    ops = Ops(Engine(proj))
    with pytest.raises(EngineError, match="no clock yet"):
        ops.run_action("asic_estimate")  # (the sdc step has not run)
    await ops.engine.run(yes=True, stop="sdc")
    (proj.root / "lib.db").unlink()
    with pytest.raises(EngineError, match="no library"):
        Ops(Engine(Project.open(proj.root))).run_action("asic_estimate")


def test_the_fpga_flow_can_ask_for_the_estimate(tmp_path):
    from q3tui import flows

    assert "asic_estimate" in flows.load_flow("fpga", Path.cwd()).actions and "asic_estimate" in flows.load_flow("asic", Path.cwd()).actions


async def test_e_in_the_sdc_tab_estimates(proj):
    from q3tui.flows.fpga import panels as fp

    eng = Engine(proj)
    await eng.run(yes=True, stop="sdc")
    app = Q3TUIApp(proj)
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause()
        ed = app.screen.query_one(fp.SdcEditor)
        ed.action_estimate()
        for _ in range(40):
            await pilot.pause(0.25)
            if (proj.schemas_dir / "asic_estimate.json").is_file():
                break
        await pilot.pause(0.5)
        (row,) = fp.SECTIONS[0].rows(app)
        assert str(row[8]) == "~621.1"  # ~ : a quick estimate


async def test_the_clock_dialog_has_an_estimate_button(proj):
    from textual.widgets import Button

    from q3tui.flows.fpga import panels as fp
    from q3tui.tui.screens import EditScreen

    eng = Engine(proj)
    await eng.run(yes=True, stop="sdc")
    app = Q3TUIApp(proj)
    async with app.run_test(size=(150, 50)) as pilot:
        await pilot.pause()
        ed = app.screen.query_one(fp.SdcEditor)
        ed.refresh_view()
        await pilot.pause()
        ed.action_edit()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, EditScreen) and screen.title_text == "Clock i_clk"
        assert [str(b.label) for b in screen.query(Button)] == ["Save (ctrl+s)", "Cancel (esc)", "Estimate ASIC Fmax"]
        screen.query_one("#estimate", Button).press()
        for _ in range(40):
            await pilot.pause(0.25)
            if "621.1" in str(screen.query_one("#edit-status").content):
                break
        status = str(screen.query_one("#edit-status").content)
        assert "i_clk ≈ 621.1 MHz" in status and "Set the ASIC frequency" in status and isinstance(app.screen, EditScreen)  # still open: the form is where you set it
        screen.dismiss(None)


async def test_the_other_forms_have_no_extra_buttons(proj):
    from textual.widgets import Button

    from q3tui.flows.fpga import panels as fp
    from q3tui.tui.screens import EditScreen

    eng = Engine(proj)
    await eng.run(yes=True, stop="sdc")
    app = Q3TUIApp(proj)
    async with app.run_test(size=(150, 50)) as pilot:
        await pilot.pause()
        ed = app.screen.query_one(fp.SdcEditor)
        ed.refresh_view()
        ed.query_one("#sdc-sections").move_cursor(row=2)  # I/O
        await pilot.pause()
        assert ed.spec.id == "io"
        ed.action_edit()
        await pilot.pause()
        assert isinstance(app.screen, EditScreen) and [str(b.label) for b in app.screen.query(Button)] == ["Save (ctrl+s)", "Cancel (esc)"]


async def test_the_estimate_button_uses_the_library_typed_in_the_form(proj):
    from textual.widgets import Button, Input

    from q3tui.flows.fpga import panels as fp
    from q3tui.flows.fpga.lib import hints
    from q3tui.tui.screens import EditScreen

    (proj.root / "lib2.db").write_text("another corner")
    eng = Engine(proj)
    await eng.run(yes=True, stop="sdc")
    app = Q3TUIApp(proj)
    async with app.run_test(size=(150, 50)) as pilot:
        await pilot.pause()
        ed = app.screen.query_one(fp.SdcEditor)
        ed.refresh_view()
        await pilot.pause()
        ed.action_edit()
        await pilot.pause()
        assert isinstance(app.screen, EditScreen)
        field = app.screen.query_one("#f-asic_lib", Input)  # not a catalogued library: its path
        assert field.value.endswith("lib.db")
        field.value = str(proj.root / "lib2.db")
        app.screen.query_one("#estimate", Button).press()
        for _ in range(40):
            await pilot.pause(0.25)
            if (proj.schemas_dir / "asic_estimate.json").is_file():
                break
    assert hints.asic_library(proj) == str(proj.root / "lib2.db")  # saved for both flows
    d = json.loads((proj.schemas_dir / "asic_estimate.json").read_text())
    assert d["library"] == [str(proj.root / "lib2.db")]  # and the estimate ran with it


# -- technologies and corners -----------------------------------------------------------------------------------------------------------


def _pdks(root: Path) -> None:
    for tech, corners in (("a", ("ss_n40C_4v50", "tt_025C_5v00", "ff_125C_5v50")), ("b", ("typ",))):
        (root / "pdk" / tech).mkdir(parents=True, exist_ok=True)
        for c in corners:
            (root / "pdk" / tech / f"cells_{tech}__{c}.db").write_text("x")
    (root / "pdk" / "a" / "macro_sram.db").write_text("not a standard-cell library")
    (root / "pdks.json").write_text(json.dumps({"only": True, "pdks": [
        {"id": "a", "title": "Tech A", "node_nm": 180, "globs": [str(root / "pdk" / "a" / "cells_a__*.db")], "default": "tt_025C_5v00"},
        {"id": "b", "title": "Tech B", "node_nm": 45, "globs": [str(root / "pdk" / "b" / "*.db")]},
        {"id": "ghost", "title": "Not installed", "node_nm": 7, "globs": [str(root / "nowhere" / "*.db")]}]}))


def test_the_catalog_of_technologies(proj):
    from q3tui.flows.asic.lib import pdks

    _pdks(proj.root)
    cat = pdks.catalog(proj)
    assert [p.id for p in cat] == ["a", "b"] and [p.label for p in cat] == ["Tech A — 180 nm", "Tech B — 45 nm"]  # the newest last; a technology without files is not offered
    assert [c for c, _ in cat[0].corners] == ["ff_125C_5v50", "ss_n40C_4v50", "tt_025C_5v00"] and [c for c, _ in cat[1].corners] == ["cells_b__typ"]  # (a single file: its name; the macro is not a corner)
    assert cat[0].default_path().endswith("cells_a__tt_025C_5v00.db")
    found = pdks.locate(str(proj.root / "pdk" / "a" / "cells_a__ss_n40C_4v50.db"), proj)
    assert found and found[0].id == "a" and found[1] == "ss_n40C_4v50" and pdks.locate("/elsewhere/x.db", proj) is None
    assert pdks.short_name(str(proj.root / "pdk" / "b" / "cells_b__typ.db"), proj) == "Tech B · cells_b__typ" and pdks.short_name("/x/y__tt.db", proj) == "tt"


def test_the_built_in_catalog_has_the_open_source_pdks_only():
    from q3tui.flows.asic.lib import pdks

    ids = {e["id"] for e in json.loads(pdks.BUILTIN.read_text())["pdks"]}
    assert ids == {"gf180", "freepdk45"}  # open-source PDKs only: licensed ones (TSMC, SAED, …) are a project's pdks.json
    gf = next(e for e in json.loads(pdks.BUILTIN.read_text())["pdks"] if e["id"] == "gf180")
    assert gf["default"] == "tt_025C_5v00" and gf["node_nm"] == 180
    default = flows.load_flow("asic", Path.cwd()).spec("asic_synth").options["library"]
    assert any(default in [p for _c, p in t.corners] for t in pdks.catalog()) or not Path(default).is_file()  # (the flow file's default is a catalogued library)


async def test_the_clock_dialog_picks_technology_then_corner(proj):
    from textual.widgets import Select

    from q3tui.flows.asic.lib import pdks
    from q3tui.flows.fpga import panels as fp
    from q3tui.flows.fpga.lib import hints
    from q3tui.tui.screens import EditScreen

    _pdks(proj.root)
    Ops(Engine(proj)).apply_settings({hints.LIBRARY_OPTION: str(proj.root / "pdk" / "a" / "cells_a__tt_025C_5v00.db")}, save=True)
    proj = Project.open(proj.root)
    await Engine(proj).run(yes=True, stop="sdc")
    app = Q3TUIApp(proj)
    async with app.run_test(size=(150, 55)) as pilot:
        await pilot.pause()
        ed = app.screen.query_one(fp.SdcEditor)
        ed.refresh_view()
        await pilot.pause()
        ed.action_edit()
        await pilot.pause()
        scr = app.screen
        assert isinstance(scr, EditScreen)
        tech, corner = scr.query_one("#f-asic_pdk", Select), scr.query_one("#f-asic_corner", Select)
        opts = lambda s: [v for _l, v in s._options if v != Select.BLANK]  # noqa: E731
        assert tech.value == "Tech A — 180 nm" and opts(tech) == ["Tech A — 180 nm", "Tech B — 45 nm"] and corner.value == "tt_025C_5v00"
        assert opts(corner) == ["ff_125C_5v50", "ss_n40C_4v50", "tt_025C_5v00"]
        tech.value = "Tech B — 45 nm"  # another technology: its own corners
        await pilot.pause()
        assert opts(corner) == ["cells_b__typ"] and corner.value == "cells_b__typ"
        scr.action_save()
        await pilot.pause()
    assert hints.asic_library(proj) == str(proj.root / "pdk" / "b" / "cells_b__typ.db")  # saved for both flows
    assert "cells_b__typ.db" in (proj.root / "q3tui.yaml").read_text() and pdks.short_name(hints.asic_library(proj), proj) == "Tech B · cells_b__typ"


async def test_the_hint_names_the_technology(proj):
    from q3tui.flows.fpga.lib import hints

    _pdks(proj.root)
    lib = str(proj.root / "pdk" / "a" / "cells_a__tt_025C_5v00.db")
    Ops(Engine(proj)).apply_settings({hints.LIBRARY_OPTION: lib}, save=True)
    proj = Project.open(proj.root)
    await Engine(proj).run(yes=True, stop="asic_synth")
    assert hints.asic_note(proj, "i_clk") == "ASIC ≈ 178.3 MHz (Tech A · tt_025C_5v00)"


FAKE_FAST_DC = """import re, shutil, sys
from pathlib import Path
text = Path(sys.argv[1]).read_text()
work, data = Path(sys.argv[2]), Path(%r)
for n in ('area', 'timing', 'qor', 'power', 'constraints', 'check_design', 'check_timing'):
    shutil.copy(data / (n + '.rpt'), work / (n + '.rpt'))
probe = Path(re.search(r'source -echo "([^"]+probe.tcl)"', text).group(1)).read_text()
period = float(re.search(r'-period ([\\d.]+)', probe).group(1))
slack = round(period - 0.8, 2)  # a fast library: the worst path takes 0.8 ns
(work / 'qor.rpt').write_text("  Timing Path Group 'i_clk'\\n  Levels of Logic: 5.00\\n  Critical Path Slack: %%s\\n  Critical Path Clk Period: %%s\\n  Total Negative Slack: 0.00\\n" %% (slack, period))
net = Path(re.search(r'write -format verilog -hierarchy -output "([^"]+)"', text).group(1))
net.parent.mkdir(parents=True, exist_ok=True)
Path(work / 'probes.txt').open('a').write(str(period) + '\\n')
print('Design Compiler Version: V-2023.12-SP3')
"""


async def test_a_fast_library_gets_a_tighter_probe(proj):
    (proj.root / "fake_dc.py").write_text(FAKE_FAST_DC % str(DATA))
    ops = Ops(Engine(proj))
    await ops.engine.run(yes=True, stop="sdc")
    msg = ops.run_action("asic_estimate")
    probes = (proj.state_dir / "asic" / "estimate" / "probes.txt").read_text().split()
    assert probes == ["1.0", "0.4"]  # the 1 ns probe is met (slack +0.2: only "at least"), the 0.4 ns one is not (slack -0.4)
    assert "i_clk ≈ 1250 MHz" in msg  # 1000 / (0.4 + 0.4)
    d = json.loads((proj.schemas_dir / "asic_estimate.json").read_text())
    assert d["probe_ns"] == 0.4 and d["timing"]["groups"][0]["bound"] == "estimate"


async def test_the_probe_starts_from_the_technologys_catalog_entry(proj):
    from q3tui.flows.fpga.lib import hints

    _pdks(proj.root)
    cat = json.loads((proj.root / "pdks.json").read_text())
    cat["pdks"][0]["probe_ns"] = 0.3
    (proj.root / "pdks.json").write_text(json.dumps(cat))
    (proj.root / "fake_dc.py").write_text(FAKE_FAST_DC % str(DATA))
    Ops(Engine(proj)).apply_settings({hints.LIBRARY_OPTION: str(proj.root / "pdk" / "a" / "cells_a__tt_025C_5v00.db")}, save=True)
    ops = Ops(Engine(Project.open(proj.root)))
    await ops.engine.run(yes=True, stop="sdc")
    msg = ops.run_action("asic_estimate")
    assert (proj.state_dir / "asic" / "estimate" / "probes.txt").read_text().split() == ["0.3"]  # (0.3 ns misses the 0.8 ns path: no second run)
    assert "i_clk ≈ 1250 MHz in Tech A · tt_025C_5v00" in msg  # 1000 / (0.3 + 0.5: the 0.8 ns path against a 0.3 ns probe)


async def test_the_best_run_of_a_clock_counts(proj):
    (proj.root / "fake_dc.py").write_text((FAKE_FAST_DC % str(DATA)).replace("slack = round(period - 0.8, 2)", "slack = round(period - (1.0 if period < 0.5 else 0.8), 2)"))
    ops = Ops(Engine(proj))
    await ops.engine.run(yes=True, stop="sdc")
    ops.run_action("asic_estimate")  # 1.0 met (0.8 ns), 0.4 a worse result (1.0 ns): the better one stays
    d = json.loads((proj.schemas_dir / "asic_estimate.json").read_text())
    assert d["timing"]["groups"][0]["period_ns"] == 1.0 and d["timing"]["groups"][0]["bound"] == "at_least" and d["timing"]["fmax_mhz"] == 1250.0


def test_a_projects_catalog_adds_its_licensed_libraries(proj):
    from q3tui.flows.asic.lib import pdks

    (proj.root / "pdk" / "lic").mkdir(parents=True)
    for c in ("ss_1p08v", "tt_1p2v"):
        (proj.root / "pdk" / "lic" / f"lic65_{c}.db").write_text("x")
    (proj.root / "pdks.json").write_text(json.dumps({"pdks": [{"id": "lic65", "title": "Licensed 65 nm", "node_nm": 65, "globs": [str(proj.root / "pdk" / "lic" / "*.db")]}]}))
    ids = [p.id for p in pdks.catalog(proj)]
    assert "lic65" in ids  # (the built-in open-source ones stay when they exist here: no "only")
    assert "lic65" not in [p.id for p in pdks.catalog()] and "lic65" not in pdks.BUILTIN.read_text()  # (and the repo never lists it)
