"""The FPGA flow on Intel parts: Quartus Prime (lib/quartus.py) — the project and timing scripts, the report readers (on outputs recorded from
Quartus Prime Pro 25.1 on an Arria 10), and the flow run with a stand-in tool that replays them. The part decides the family (lib/templates.py)."""

import json
import sys
from pathlib import Path

import pytest

from q3tui.core.project import Project
from q3tui.flows.fpga.lib import quartus, templates
from q3tui.pipeline.base import StepFailed
from q3tui.pipeline.engine import Engine

DATA = Path(__file__).parent / "data" / "fpga" / "quartus"
RTL = """module blk (input i_clk, input i_rst_n, input [3:0] i_a, input i_v, output reg [3:0] o_y);
  always @(posedge i_clk or negedge i_rst_n) if (!i_rst_n) o_y <= 0; else if (i_v) o_y <= i_a;
endmodule
"""


def test_the_scripts_for_pro_and_standard(tmp_path):
    a10, _dev, _ = templates.resolve({"part": "10AX115N2F40E2LG"})
    cv, _dev, _ = templates.resolve({"part": "5CEBA4F23C7"})
    files, sdc = [tmp_path / "rtl" / "blk.sv", tmp_path / "rtl" / "old.v"], [tmp_path / "sdc" / "clocks.tcl"]
    tcl = quartus.script_text(files, "blk", a10, "10AX115N2F40E2LG", sdc, tmp_path, {"defines": {"SYNTH": None, "W": 8}})
    assert 'set_global_assignment -name FAMILY "Arria 10"' in tcl and "set_global_assignment -name DEVICE 10AX115N2F40E2LG" in tcl
    assert f"SYSTEMVERILOG_FILE {{{files[0]}}}" in tcl and f"VERILOG_FILE {{{files[1]}}}" in tcl and f"SDC_FILE {{{sdc[0]}}}" in tcl
    assert f"SEARCH_PATH {{{tmp_path / 'rtl'}}}" in tcl                                    # (a memory's ram_init_file beside its RTL)
    assert "VERILOG_MACRO {SYNTH}" in tcl and "VERILOG_MACRO {W=8}" in tcl
    assert "exec quartus_syn q3tui_fpga" in tcl and "quartus_fit" not in tcl and "load_package flow" not in tcl   # (the tools, not the flow package)
    assert "exec quartus_map q3tui_fpga" in quartus.script_text(files, "blk", cv, "5CEBA4F23C7", sdc, tmp_path, {"implement": True})
    assert "exec quartus_fit q3tui_fpga" in quartus.script_text(files, "blk", cv, "5CEBA4F23C7", sdc, tmp_path, {"implement": True})
    assert "create_timing_netlist -snapshot synthesized" in quartus.sta_text(a10, sdc, tmp_path, False)
    assert "create_timing_netlist -post_map" in quartus.sta_text(cv, sdc, tmp_path, False)
    sta = quartus.sta_text(a10, sdc, tmp_path, True)
    assert "create_timing_netlist\n" in sta and f"read_sdc {{{sdc[0]}}}" in sta and "Q3TUI WNS" in sta and "get_clock_domain_info -setup" in sta


def test_the_reports_recorded_from_quartus_pro():
    util = quartus.parse_utilization((DATA / "syn.summary").read_text(), (DATA / "entity.rpt").read_text())
    assert util["lut"] == {"used": 17, "available": 427200, "pct": 0.004, "label": "ALMs"}          # ("< 1 %": the real share)
    assert util["ff"]["used"] == 16 and util["ff"]["available"] is None and util["io"]["used"] == 18 and util["mem_bits"]["used"] == 0
    t = quartus.parse_timing((DATA / "timing.rpt").read_text(), (DATA / "fmax.rpt").read_text())
    assert t == {"wns_ns": 2.666, "whs_ns": 0.024, "tns_ns": 0.0, "fmax_mhz": 749.63, "met": True}
    cp = quartus.critical_path((DATA / "timing.rpt").read_text())
    assert cp == {"slack_ns": 2.666, "source": "pc[0]", "destination": "q[7]", "logic_levels": 4, "data_path_ns": 1.503}
    assert quartus.tool_version("    Info: Version 25.1.0 Build 129 03/26/2025 Patches 0.36 SC Pro Edition") == "Quartus Prime 25.1.0 Pro"
    # the table of the report, not its contents line nor a setting of the same name
    report = ("       6. Partition \"root_partition\" Resource Utilization by Entity\n"
              "; Report Resource Utilization by Entity to ASCII ; On ; On ;\n\n" + (DATA / "entity.rpt").read_text() + "\n\nmore\n")
    assert quartus.entity_top(report)["Pins"] == 18


@pytest.fixture
def proj(tmp_path):
    (tmp_path / "src" / "rtl").mkdir(parents=True)
    (tmp_path / "src" / "rtl" / "blk.sv").write_text(RTL)
    (tmp_path / "src" / "rtl" / "rom.sv").write_text('module rom (input i_clk, output [7:0] o_d);\n  (* ram_init_file = "rom.mif" *) logic [7:0] mem [0:255];\n'
                                                      "  assign o_d = mem[0];\nendmodule\n")
    (tmp_path / "src" / "rtl" / "filelist.f").write_text("src/rtl/rom.sv\nsrc/rtl/blk.sv\n")
    (tmp_path / "sw").mkdir()
    (tmp_path / "sw" / "rom.hex").write_text("// the boot image\n0a\n0b\n0c\n")  # (a $readmemh image, as a firmware build writes it)
    (tmp_path / "schemas").mkdir()
    (tmp_path / "schemas" / "structured_spec.json").write_text(json.dumps({"metadata": {"top_module": "blk"}}))
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec" / "spec.md").write_text("| **Clock** | `i_clk`, target frequency 200 MHz (5 ns period) | Intel Arria 10 10AX115N2F40E2LG |\n")
    # a stand-in for quartus_sh: the outputs Quartus Prime Pro 25.1 wrote for a small design, into the work folder
    (tmp_path / "fake_quartus.py").write_text(
        "import shutil, sys\nfrom pathlib import Path\n"
        f"d, w = Path({str(DATA)!r}), Path(sys.argv[2])\n"
        "shutil.copy(d / 'syn.summary', w / 'q3tui_fpga.syn.summary'); shutil.copy(d / 'entity.rpt', w / 'q3tui_fpga.syn.rpt')\n"
        "shutil.copy(d / 'timing.rpt', w / 'timing.rpt'); shutil.copy(d / 'fmax.rpt', w / 'fmax.rpt'); (w / 'paths.rpt').write_text('paths')\n"
        "print('    Info: Version 25.1.0 Build 129 03/26/2025 Patches 0.36 SC Pro Edition')\n"
        "print('Info: Quartus Prime Synthesis was successful. 0 errors, 0 warnings')\nprint('Warning (13469): a warning')\n")
    (tmp_path / "q3tui.yaml").write_text(
        "pipeline: {flow: fpga, step_options: {fpga_synth: {part: 10AX115N2F40E2LG}}}\ntools:\n  roles:\n    fpga_quartus_pro:\n"
        f"      cmd: \"{sys.executable} {tmp_path / 'fake_quartus.py'} {{script}} {{workdir}}\"\n"
        "      parse: {error: ['^Error'], warning: ['^Warning', '^Critical Warning']}\n")
    return Project.open(tmp_path)


class PortLLM:
    """The porter (`port(stage)`: it may call the stage's own tools, as the agent would) and the judge (offline: not judged)."""

    def __init__(self, port):
        self.port, self.stages = port, []

    async def __call__(self, stage, cfg, emit):
        from q3tui.llm.runtime import StageResult

        self.stages.append(stage)
        if stage.name != "fpga_port":
            raise RuntimeError("no credentials")
        out = await self.port(stage)
        emit.add_cost(0.01)
        return StageResult("ok", out, 0.01, 2, "s")

    def ported(self) -> list:
        return [s for s in self.stages if s.name == "fpga_port"]


async def _tool(stage, name: str, **args) -> str:
    t = next(t for t in stage.sdk_tools if t.name == name)
    return (await t.handler(args))["content"][0]["text"]


async def test_the_port_makes_the_memory_image_and_quartus_synthesizes(proj, monkeypatch):
    from q3tui.flows.fpga.lib import port
    from q3tui.llm import runtime

    async def porter(stage):
        assert "[init_missing] src/rtl/rom.sv:2 (rom)" in stage.prompt and "- sw/rom.hex — 3 word(s), 4 bit(s) used" in stage.prompt
        said = await _tool(stage, "mem_image", source="sw/rom.hex", target="rom.mif", depth=256, width=8)
        assert said == "wrote src/fpga/rom.mif: 3 word(s) of 8 bits, depth 256 (the rest 0)", said
        assert await _tool(stage, "port_scan") == "(nothing)"                                    # (checked: the file is found now)
        assert (await _tool(stage, "mem_image", source="../outside.hex", target="rom.mif", depth=256, width=8)).startswith("no image")
        return port.PortResult(summary="rom.mif made from the boot image sw/rom.hex")

    fake = PortLLM(porter)
    monkeypatch.setattr(runtime, "run_stage", fake)
    seen = []
    eng = Engine(proj)
    eng.bus.subscribe(lambda e: seen.append(e))
    await eng.run()  # (stops at the report's human gate)
    st = {v.name: (v.status, v.detail) for v in eng.status()}
    assert st["fpga_port"][0] == "done" and st["fpga_synth"][0] == "done" and st["timing_util_report"][0] == "done", st
    (stage,) = fake.ported()
    assert stage.write_dirs == [proj.fpga_src_dir] and {"Write", "Edit"} <= set(stage.builtin_tools) and "fpga-port" in stage.prompt
    mif = (proj.root / "src" / "fpga" / "rom.mif").read_text()
    assert "WIDTH=8;" in mif and "DEPTH=256;" in mif and "\t0 : 0A;" in mif and "\t2 : 0C;" in mif and "\t[3..FF] : 00;" in mif
    assert (proj.state_dir / "fpga" / "fpga_synth" / "rom.mif").read_text() == mif              # (into Quartus' project folder: Pro reads it there)
    rep = json.loads((proj.schemas_dir / "fpga_port.json").read_text())
    assert rep["scan"] == [] and rep["family"] == "arria10" and rep["done"] == ["src/fpga/rom.mif ← sw/rom.hex: 3 word(s) of 8 bits, depth 256 (the rest 0)"]
    s = json.loads((proj.schemas_dir / "fpga_synth.json").read_text())
    assert (s["status"], s["backend"], s["family"], s["part"], s["tool"], s["template"]) == ("pass", "quartus", "Arria 10", "10AX115N2F40E2LG", "Quartus Prime 25.1.0 Pro", "arria10")
    tcl = (proj.root / "build" / "syn" / "fpga" / "synth.tcl").read_text()
    assert 'FAMILY "Arria 10"' in tcl and "exec quartus_syn" in tcl and "rom.sv" in tcl
    rep = proj.root / "build" / "syn" / "fpga" / "reports"
    assert {"utilization.rpt", "utilization_hier.rpt", "timing.rpt", "clocks.rpt", "synth.log"} <= {f.name for f in rep.iterdir()}
    ppa = json.loads((proj.schemas_dir / "ppa_report.json").read_text())
    assert ppa["utilization"]["lut"]["label"] == "ALMs" and ppa["utilization"]["lut"]["used"] == 17
    assert ppa["timing"]["wns_ns"] == 2.666 and ppa["timing"]["fmax_mhz"] == 749.63 and ppa["critical_path"]["source"] == "pc[0]"
    assert ppa["verdict"]["timing"] == "pass" and ppa["verdict"]["hold"] == "not judged (post-synthesis)" and ppa["verdict"]["plausibility"] == "not judged"
    assert any("ALMs" in str(e.data.get("message")) for e in seen if e.kind == "log")
    eng2 = Engine(Project.open(proj.root))
    await eng2.run()  # nothing new: the porter is not asked again, nothing runs again
    assert len(fake.ported()) == 1 and {v.name: v.status for v in eng2.status()}["fpga_port"] == "done"


async def test_a_failed_synthesis_goes_to_the_port_and_runs_again(proj, monkeypatch):
    from q3tui.flows.fpga.lib import port
    from q3tui.llm import runtime

    (proj.root / "src" / "fpga").mkdir()
    (proj.root / "src" / "fpga" / "rom.mif").write_text("WIDTH=8;\nDEPTH=256;\nCONTENT BEGIN\nEND;\n")  # (nothing for the scan)
    fq = proj.root / "fake_quartus.py"
    fq.write_text("import sys\nfrom pathlib import Path\nif 'VERILOG_MACRO {USE_RAM}' not in Path(sys.argv[1]).read_text():\n"
                  "    print('Error (12006): Node instance \"u_ram\" instantiates undefined entity \"ram_ip\". File: blk.sv Line: 3')\n"
                  "    print('Error: Quartus Prime Synthesis was unsuccessful. 1 error, 0 warnings')\n    sys.exit(1)\n" + fq.read_text())

    async def porter(stage):
        assert "From fpga_synth (Quartus Prime Pro, Arria 10 10AX115N2F40E2LG): synthesis failed" in stage.prompt
        assert "Error (12006): Node instance \"u_ram\" instantiates undefined entity" in stage.prompt
        bad = await _tool(stage, "synth_setting", key="synth_args", value="set_global_assignment -name SEED [exec rm -rf ~]")
        assert bad.startswith("not changed: synth_args") and (await _tool(stage, "synth_setting", key="part", value="5CEBA4F23C7")).startswith("not changed")
        assert await _tool(stage, "synth_setting", key="defines", value="USE_RAM") == "fpga_synth defines = {'USE_RAM': None} (saved to q3tui.yaml)"
        return port.PortResult(summary="the RAM is the inferred one when USE_RAM is defined for synthesis")

    fake = PortLLM(porter)
    monkeypatch.setattr(runtime, "run_stage", fake)
    eng = Engine(proj)
    await eng.run()  # fpga_synth fails → fpga_port (the fix loop) → fpga_synth again → the report's gate
    st = {v.name: (v.status, v.detail) for v in eng.status()}
    assert st["fpga_synth"][0] == "done" and st["timing_util_report"][0] == "done", st
    assert len(fake.ported()) == 1 and json.loads((proj.schemas_dir / "fpga_synth.json").read_text())["status"] == "pass"
    assert "VERILOG_MACRO {USE_RAM}" in (proj.root / "build" / "syn" / "fpga" / "synth.tcl").read_text() and "USE_RAM" in (proj.root / "q3tui.yaml").read_text()
    rep = json.loads((proj.schemas_dir / "fpga_port.json").read_text())
    assert rep["done"] == ["fpga_synth defines = {'USE_RAM': None} (saved to q3tui.yaml)"] and "Error (12006)" in rep["requests"][0]
    # out of rounds: the synthesis fails with the tool's error, nothing is handed on
    (proj.root / "q3tui.yaml").write_text((proj.root / "q3tui.yaml").read_text().replace("USE_RAM", "OTHER"))
    eng = Engine(Project.open(proj.root))
    eng.by_name["fpga_synth"].options["max_rounds"] = 0
    with pytest.raises(StepFailed, match=r"Error \(12006\).*still failing after 0 round"):
        await eng.run_step(eng.by_name["fpga_synth"])
    assert json.loads((proj.schemas_dir / "fpga_synth.json").read_text())["status"] == "failed"
    assert not (proj.root / "build" / "syn" / "fpga" / "reports" / "utilization.rpt").exists()       # (a failed run leaves no old numbers)
    assert "the synthesis failed" in Engine(Project.open(proj.root)).by_name["timing_util_report"].missing_input(eng)


async def test_the_port_asks_what_is_yours_to_decide_and_acts_on_the_answer(proj, monkeypatch):
    from q3tui.flows.fpga.lib import port
    from q3tui.llm import runtime
    from q3tui.steps.common import Question, QuestionOption

    (proj.root / "sw" / "demo.hex").write_text("ff\nee\n")
    labels = ["Boot rom from sw/rom.hex (the boot image)", "Boot rom from sw/demo.hex (the demo)"]

    async def porter(stage):
        if "→ " + labels[1] not in stage.prompt:
            return port.PortResult(summary="two images fit rom.mif and the spec does not say which one", questions=[Question(
                id="", question="Which program does rom boot with on the FPGA?", blocking=True, default_assumption=labels[0], kind="design_choice",
                options=[QuestionOption(label=x) for x in labels])])
        await _tool(stage, "mem_image", source="sw/demo.hex", target="rom.mif", depth=256, width=8)
        return port.PortResult(summary="rom.mif from sw/demo.hex, as decided")

    fake = PortLLM(porter)
    monkeypatch.setattr(runtime, "run_stage", fake)
    eng = Engine(proj)
    await eng.run()
    (q,) = [q for q in eng.questions() if q["step"] == "fpga_port"]
    assert q["id"] == "FP1" and q["blocking"] and q["status"] == "open" and [o["label"] for o in q["options"]] == labels
    assert {v.name: v.status for v in eng.status()}["fpga_synth"] != "done"                        # (it waits for the decision)
    eng.answer("FP1", labels[1])
    eng = Engine(Project.open(proj.root))
    await eng.run()
    assert len(fake.ported()) == 2 and "FP1: Which program does rom boot with on the FPGA? → " + labels[1] in fake.ported()[1].prompt
    assert "\t0 : FF;" in (proj.root / "src" / "fpga" / "rom.mif").read_text()
    assert {v.name: v.status for v in eng.status()}["fpga_synth"] == "done"


def test_images_become_the_file_the_rtl_names(tmp_path):
    from q3tui.flows.fpga.lib import port

    img = tmp_path / "fw.imem.hex"
    img.write_text("00000013\n00000093 // nop\n/* a gap */\n@10\nDEAD_BEEF\n")
    mif, what = port.convert(img, "m_imem.mif", 32, 32)
    assert what == "3 word(s) of 32 bits, depth 32 (the rest 0)"
    assert mif.splitlines()[1:] == ["WIDTH=32;", "DEPTH=32;", "ADDRESS_RADIX=HEX;", "DATA_RADIX=HEX;", "CONTENT BEGIN", "\t0 : 00000013;", "\t1 : 00000093;",
                                    "\t[2..F] : 00000000;", "\t10 : DEADBEEF;", "\t[11..1F] : 00000000;", "END;"]
    (tmp_path / "back.mif").write_text(mif)
    assert port.read_image(tmp_path / "back.mif") == {**{a: 0 for a in range(32)}, 0: 0x13, 1: 0x93, 16: 0xDEADBEEF}
    assert port.convert(img, "m_imem.hex", 32, 32)[0] == "00000013\n00000093\n@10\nDEADBEEF\n"
    (tmp_path / "fw.bin").write_bytes(bytes([0x13, 0, 0, 0, 0x93, 0, 0, 0]))
    assert port.read_image(tmp_path / "fw.bin", 32) == {0: 0x13, 1: 0x93}
    with pytest.raises(ValueError, match="deeper than the memory"):
        port.convert(img, "m_imem.mif", 8, 32)
    with pytest.raises(ValueError, match="wider than 16 bits"):
        port.convert(img, "m_imem.mif", 32, 16)


def test_the_scan_says_what_the_parts_tool_cannot_take(proj):
    from q3tui.flows.fpga.lib import port

    (proj.root / "src" / "rtl" / "pll.sv").write_text("module pll (input i);\n  MMCME2_BASE #(.CLKIN1_PERIOD(10)) u_mmcm (.CLKIN1(i));\n"
                                                      "  altsyncram u_ram (.clock0(i));\nendmodule\n")
    files = sorted((proj.root / "src" / "rtl").glob("*.sv"))
    on_intel = {(x.rule, x.module, x.line) for x in port.scan(proj, files, templates.get("arria10"))}
    assert on_intel == {("init_missing", "rom", 2), ("foreign_primitive", "pll", 2)}
    on_xilinx = port.scan(proj, files, templates.get("xilinx7"))
    assert {(x.rule, x.module) for x in on_xilinx} == {("init_ignored", "rom"), ("foreign_primitive", "pll")}
    assert any("Vivado ignores Quartus' ram_init_file" in x.note for x in on_xilinx) and any("altsyncram is an Intel primitive" in x.note for x in on_xilinx)


def test_a_part_no_family_synthesizes_fails_the_synthesis_with_why(proj):
    import anyio

    (proj.root / "q3tui.yaml").write_text((proj.root / "q3tui.yaml").read_text().replace("10AX115N2F40E2LG", "xczu9eg-ffvb1156-2-e"))
    eng = Engine(Project.open(proj.root))
    with pytest.raises(StepFailed, match="no FPGA family here synthesizes it"):
        anyio.run(lambda: eng.run_step(eng.by_name["fpga_synth"]))
