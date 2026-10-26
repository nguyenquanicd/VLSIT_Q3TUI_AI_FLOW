"""Milestone M4: the existing VIP is probed (never invented), bound to the interface contract, and wired into the scaffold.
The deterministic tests use a small stand-in VIP (tests/fixtures/mini_axi_vip); the real axi4_avip is used when AXI_VIP_HOME names it."""

import json
import os
import shutil
from pathlib import Path

import pytest

from q3tui.flows.dv.uvm import binding as B
from q3tui.flows.dv.uvm import contract as C
from q3tui.flows.dv.uvm import lint as L
from q3tui.flows.dv.uvm import scaffold as S
from q3tui.flows.dv.uvm import vip as V
from tests.test_dut_dv_intent import EXAMPLE, engine, intent_from_contract, llm, load_contract, run_tb  # noqa: F401  (fixtures)
from tests.test_dut_dv_scaffold import OPTS, baseline

MINI = Path(__file__).resolve().parent / "fixtures" / "mini_axi_vip"
REAL = os.environ.get("AXI_VIP_HOME")


def contract() -> C.VerificationContract:
    return baseline()[0]


def test_probe_learns_the_vip_from_its_source():
    p = V.probe(MINI, "custom", path_expr="$AXI_VIP_HOME")
    assert V.conformance(p) == []
    assert p["roles"]["agent"] == "axi4_master_agent" and p["roles"]["item"] == "axi4_master_tx"
    assert p["agent_members"] == {"agent_config": "axi4_master_agent_cfg_h", "write_sequencer": "axi4_master_write_seqr_h", "read_sequencer": "axi4_master_read_seqr_h"}
    assert [u["unit"] for u in p["compile"]["order"]] == ["axi4_globals_pkg", "axi4_master_pkg", "axi4_if", "axi4_master_driver_bfm", "axi4_master_monitor_bfm",
                                                          "master_assertions", "axi4_master_agent_bfm"]
    assert p["globals"]["parameters"]["DATA_WIDTH"]["value"] == "64" and "AXI_MODE_slave" in p["globals"]["mode_macros"]
    assert p["enums"]["transfer_type_e"] == ["OUTSTANDING_WRITE", "OUTSTANDING_READ", "NON_OUTSTANDING_WRITE", "NON_OUTSTANDING_READ"]
    assert p["bfms"]["axi4_master_driver_bfm"]["ports"][2] == {"dir": "output", "width": "[3:0]", "name": "awid"}
    assert {i["id"] for i in p["known_issues"]} == {"ZERO_BEAT_DROPPED", "ACTIVE_NEEDS_COVERAGE"}
    assert list(p["strong_sva_units"]) == ["master_assertions"]
    # portable: the artifact carries the path as written in the options, never this machine's absolute location
    assert p["vip"]["path"] == "$AXI_VIP_HOME" and str(MINI) not in json.dumps(p)


def test_probe_reports_a_vip_it_cannot_use(tmp_path):
    (tmp_path / "empty.sv").write_text("module m; endmodule\n")
    bad = V.conformance(V.probe(tmp_path))
    assert any("no agent class" in b for b in bad) and any("no compile closure" in b for b in bad)


def test_binding_overrides_the_vip_constant_and_substitutes_what_the_simulator_rejects():
    c = contract()
    p = V.probe(MINI)
    b = B.derive(p, c)
    assert b["config_overrides"] == {"ADDRESS_WIDTH": 32, "DATA_WIDTH": 32} and b["blocking"] == []
    assert b["dut_role"] == "slave" and b["vip_agent_role"] == "master" and b["mode_macro"] == "AXI_MODE_slave"
    sm = {m["dut"]: m for m in b["signal_map"]}
    assert len(sm) == 29 and all(m["status"] == "matched" for m in sm.values())
    assert sm["i_awaddr"]["direction"] == "vip_to_dut" and sm["o_bvalid"]["direction"] == "dut_to_vip" and sm["i_wdata"]["vip_width"] == 32
    assert b["tie_to_zero"] == ["buser", "ruser"] and "awlock" in b["unconnected_vip_outputs"]
    assert {(s["type"], s["unit"]) for s in b["substitutions"]} == {("unit", "master_assertions"), ("include", "axi4_master_coverage")}
    assert b["agent_cfg"] == {"has_coverage": 1} and b["compile_options"] == {"vcs": ["-suppress=ETMFCB"]}
    assert b["observation"]["source"] == "tb_pin_monitor" and "ZERO_BEAT_DROPPED" in b["observation"]["reason"]
    assert any("DATA_WIDTH" in f["text"] and "not edited" in f["text"] for f in b["findings"])


def test_binding_reports_a_width_that_cannot_match():
    c = contract()
    ports = [p.model_copy(update={"width": "8"}) if p.name == "i_awid" else p for p in c.interface_contract.ports]
    c2 = c.model_copy(update={"interface_contract": c.interface_contract.model_copy(update={"ports": ports})})
    b = B.derive(V.probe(MINI), c2)
    assert b["blocking"] and "i_awid" in b["blocking"][0]["text"] and "width_mismatch" in b["blocking"][0]["text"]


def test_width_evaluation():
    assert B.safe_eval("PR_DATA_WD/8", {"PR_DATA_WD": 32}) == 4 and B.safe_eval("[(DATA_WIDTH/8)-1:0]", {"DATA_WIDTH": 64}) == 8
    assert B.safe_eval("[3:0]", {}) == 4 and B.safe_eval("1", {}) == 1 and B.safe_eval("", {}) == 1 and B.safe_eval("__import__('os')", {}) is None


def vip_files(tmp_path):
    c, a = baseline()
    p = V.probe(MINI, "custom", path_expr="$AXI_VIP_HOME")
    b = B.derive(p, c)
    f = S.generate(c, a, OPTS, "demo", "engineer", "2026-10-19", vip={"probe": p, "binding": b, "root": "$AXI_VIP_HOME", "dir": MINI})
    return c, p, b, f


def test_generation_with_the_vip(tmp_path):
    c, p, b, f = vip_files(tmp_path)
    ad = f["adapters/dut_axi_adapter.svh"]
    assert "axi4_master_pkg::axi4_master_tx" in ad and "axi4_globals_pkg::NON_OUTSTANDING_WRITE" in ad and "vip_agent.axi4_master_write_seqr_h" in ad
    assert "vip_cfg.has_coverage = 1" in ad and "task send(dut_axi_txn t)" in ad and "DV_TODO" not in ad
    br = f["integration/dut_axi_bridge.sv"]
    assert "assign pins.i_awaddr = vip.awaddr;" in br and "assign pins.i_awvalid = (vip.awvalid === 1'b1);" in br and "assign pins.i_bready = (vip.bready === 1'b1);" in br and "assign vip.bvalid = pins.o_bvalid;" in br and "assign vip.buser = '0;" in br
    assert "parameter int DATA_WIDTH = 32;  // was 64 in the VIP" in f["vip_cfg/axi4_globals_pkg.sv"]
    assert "STAND-IN" in f["vip_cfg/master_assertions_standin.sv"] and "STAND-IN" in f["vip_cfg/axi4_master_coverage.sv"]
    fl = f["filelist_tb.f"].splitlines()
    assert fl.index("+incdir+./vip_cfg") < fl.index("+incdir+$AXI_VIP_HOME/src/globals")  # the stand-in include is found first
    assert "vip_cfg/axi4_globals_pkg.sv" in fl and "$AXI_VIP_HOME/src/globals/axi4_globals_pkg.sv" not in fl and "$AXI_VIP_HOME/src/hvl_top/master/axi4_master_pkg.sv" in fl
    assert fl.index("vip_cfg/axi4_globals_pkg.sv") < fl.index("dut_tb_params_pkg.sv") and "integration/dut_axi_bridge.sv" in fl
    assert "axi4_master_agent_bfm #(.MASTER_ID(0)) u_vip_master_bfm(vip_if);" in f["tb_top.sv"] and "dut_axi_bridge u_axi_bridge" in f["tb_top.sv"]
    pm = f["env/dut_axi_pin_monitor.svh"]
    assert "vif.mon_cb.i_awvalid === 1'b1 && vif.mon_cb.o_awready === 1'b1" in pm and "class dut_axi_pin_monitor" in pm


def test_lint_keeps_the_vip_behind_the_adapter(tmp_path):
    c, p, b, f = vip_files(tmp_path)
    S.write(tmp_path, f, {})
    names = {u["unit"] for u in p["compile"]["order"] if u["kind"] == "package"} | {x["name"] for x in p["classes"]}
    root = ("$AXI_VIP_HOME", MINI)
    assert L.lint(tmp_path, c, list(f), names, root) == []
    sb = tmp_path / "scoreboard" / "dut_scoreboard.svh"
    sb.write_text(sb.read_text() + "\n  axi4_master_tx leaked;\n")
    assert any(x["code"] == "DV-V01" and "axi4_master_tx" in x["message"] for x in L.lint(tmp_path, c, list(f), names, root))


def test_step_probes_the_vip_and_records_the_binding(engine, llm):
    root = engine.project.root
    for n in ("golden_manifest", "verification_contract", "tb_architecture"):
        shutil.copy(EXAMPLE / "schemas" / f"{n}.json", root / "schemas")
    llm.dv_intents = [intent_from_contract(load_contract(root))]
    with pytest.raises(Exception, match="NOT_RUN|nothing was simulated"):
        run_tb(engine)
    vi = json.loads((root / "schemas" / "vip_integration.json").read_text())
    assert vi["binding"]["config_overrides"]["DATA_WIDTH"] == 32 and str(root) not in vi["probe"]["vip"]["path"] and vi["probe"]["vip"]["path"] == "vip"
    assert (root / "src" / "tb" / "vip_cfg" / "axi4_globals_pkg.sv").is_file() and (root / "src" / "tb" / "integration" / "dut_axi_bridge.sv").is_file()
    assert json.loads((root / "schemas" / "dv_validation.json").read_text())["errors"] == 0
    # the VIP itself was never touched
    assert "DATA_WIDTH = 64" in (root / "vip" / "src" / "globals" / "axi4_globals_pkg.sv").read_text()


def test_missing_vip_member_is_a_clear_failure(engine, llm):
    root = engine.project.root
    for n in ("golden_manifest", "verification_contract", "tb_architecture"):
        shutil.copy(EXAMPLE / "schemas" / f"{n}.json", root / "schemas")
    (root / "vip" / "src" / "hvl_top" / "master" / "axi4_master_write_sequencer.sv").unlink()
    llm.dv_intents = [intent_from_contract(load_contract(root))]
    with pytest.raises(Exception, match="cannot be used as it is"):
        run_tb(engine)


@pytest.mark.skipif(not REAL, reason="AXI_VIP_HOME is not set: the real axi4_avip is not available")
def test_real_vip_binds_to_the_axi4_sram_ctrl_contract():
    p = V.probe(Path(os.path.expandvars(REAL)), "axi4_avip", path_expr="$AXI_VIP_HOME")
    assert V.conformance(p) == []
    b = B.derive(p, contract())
    assert b["blocking"] == [] and b["config_overrides"]["DATA_WIDTH"] == 32 and len(b["signal_map"]) == 29
    assert {s["unit"] for s in b["substitutions"]} == {"master_assertions", "axi4_master_coverage"}


def test_artifact_never_carries_an_absolute_vip_path(engine, llm):
    root = engine.project.root
    for n in ("golden_manifest", "verification_contract", "tb_architecture"):
        shutil.copy(EXAMPLE / "schemas" / f"{n}.json", root / "schemas")
    cfg = (root / "q3tui.yaml").read_text().replace("{vip: {path: vip,", "{vip: {path: " + str(root / "vip") + ",")
    (root / "q3tui.yaml").write_text(cfg)
    eng = type(engine)(type(engine.project).open(root))
    llm.dv_intents = [intent_from_contract(load_contract(root))]
    with pytest.raises(Exception, match="NOT_RUN|nothing was simulated"):
        run_tb(eng)
    vi = (root / "schemas" / "vip_integration.json").read_text()
    assert str(root) not in vi and "uvm_vip.vip.path option" in vi
