"""Milestone M3: the deterministic UVM scaffold, its regions, the worklist and the validators (no simulator needed)."""

import json
import shutil
from pathlib import Path

import pytest

from q3tui.flows.dv.uvm import contract as C
from q3tui.flows.dv.uvm import evidence as ev
from q3tui.flows.dv.uvm import lint as L
from q3tui.flows.dv.uvm import scaffold as S
from q3tui.flows.dv.uvm import svgen as g
from q3tui.flows.dv.uvm import worklist as W
from tests.test_dut_dv_intent import EXAMPLE, engine, llm, load_contract, run_tb  # noqa: F401  (fixtures)

OPTS = {"vip": {"kind": "axi4_avip"}, "integration": {"smoke_test": "dut_smoke_test"}}


def baseline():
    c = C.VerificationContract.model_validate(json.loads((EXAMPLE / "schemas" / "verification_contract.json").read_text()))
    return c, json.loads((EXAMPLE / "schemas" / "tb_architecture.json").read_text())


def gen(c=None, a=None):
    c0, a0 = baseline()
    return S.generate(c or c0, a or a0, OPTS, "demo", "engineer", "2026-10-19")


def test_scaffold_is_deterministic_and_complete():
    f1, f2 = gen(), gen()
    assert f1 == f2
    assert {"tb_top.sv", "dut_dv_pkg.sv", "dut_tb_params_pkg.sv", "interfaces/dut_pins_if.sv", "integration/dut_wrapper_shell.sv", "tests/dut_test_lib.svh",
            "scoreboard/dut_scoreboard.svh", "coverage/dut_cov.svh", "assertions/dut_assertions.sv", "filelist_tb.f"} <= set(f1)
    pins = f1["interfaces/dut_pins_if.sv"]
    assert "i_awaddr" in pins and "o_sram_we" in pins and "[PARA_NUM_BANKS-1:0][PARA_ADDR_WD-1:0]" in pins  # the specification's names and widths
    assert "localparam int PARA_DATA_WD = 32" in f1["dut_tb_params_pkg.sv"]
    assert "NOT THE DUT" in f1["integration/dut_wrapper_shell.sv"] and "assign pins.o_bvalid = '0" in f1["integration/dut_wrapper_shell.sv"]


def test_every_contract_item_has_code_with_its_trace():
    c, _ = baseline()
    f = gen()
    sb, tests = f["scoreboard/dut_scoreboard.svh"], f["tests/dut_test_lib.svh"]
    for r in c.scoreboard_rules:
        assert f"// {r.id} | {', '.join(r.requirement_ids)} | {r.source}" in sb and f"DV_TODO({r.id}" in sb
    for t in c.tests:
        assert f"// {t.tc_id} | " in tests
    assert "class dut_smoke_test extends dut_base_test" in tests and 'tc_id = "TC-001"' in tests
    assert all(f"// {k.id} | " in f["coverage/dut_cov.svh"] for k in c.coverage)


def test_stubs_announce_themselves_and_block_pass():
    f = gen()
    assert "`define DV_TODO(ID, MSG) dut_dv_todo::announce" in f["dut_dv_base.svh"] and "uvm_warning(\"DV_TODO\"" in f["dut_dv_base.svh"]
    t = f["tests/dut_test_lib.svh"]
    assert "dut_dv_todo::count != 0 || chk == 0) ? \"INCOMPLETE\" : \"PASS\"" in t  # no DV_TODO and at least one check
    assert "VLSIT_DUT_TB_RESULT test=%s tc=%s status=%s checks=%0d mismatches=%0d todo=%0d" in t


def test_worklist_and_lint_on_a_fresh_scaffold(tmp_path):
    c, _ = baseline()
    f = gen()
    S.write(tmp_path, f, {})
    items = W.scan(tmp_path, c)
    s = W.summary(items)
    assert s["open"] == s["regions"] > 20 and s["agent_or_user_owned"] == 0
    kinds = {i["kind"] for i in items}
    assert {"scoreboard_rule", "coverage_goal", "test", "sequence", "sram_model", "adapter", "assertion"} <= kinds
    sbr = next(i for i in items if i["id"] == "SBR-001")
    assert sbr["requirements"] and sbr["source"] and sbr["file"].endswith("dut_scoreboard.svh")
    assert L.lint(tmp_path, c, list(f)) == []


def test_validators_catch_faults(tmp_path):
    c, _ = baseline()
    f = gen()
    S.write(tmp_path, f, {})
    sb = tmp_path / "scoreboard" / "dut_scoreboard.svh"
    sb.write_text(sb.read_text().replace("// SBR-002 |", "// SBR-902 |").replace("REQ-007", "REQ-777", 1) + "\n  x = tb_top.u_dut.u_core.q;\n\tbad\n")
    (tmp_path / "tests" / "dut_test_lib.svh").unlink()
    (tmp_path / "filelist_tb.f").write_text((tmp_path / "filelist_tb.f").read_text() + "missing/file.sv\n")
    codes = {x["code"] for x in L.lint(tmp_path, c, list(f))}
    assert {"DV-F01", "DV-F02", "DV-T01", "DV-T02", "DV-T03", "DV-I01", "DV-H01"} <= codes
    # a VIP package named outside the adapter is refused (DUT-DV-019)
    S.write(tmp_path, {"tests/dut_test_lib.svh": f["tests/dut_test_lib.svh"] + "\nimport axi4_master_pkg::*;\n"}, {})
    assert any(x["code"] == "DV-V01" for x in L.lint(tmp_path, c, list(f), {"axi4_master_pkg"}))
    # a scaffold-owned region without a DV_TODO is refused
    sb.write_text(f["scoreboard/dut_scoreboard.svh"].replace("`DV_TODO(SBR-001", "// done (SBR-001", 1))
    assert any(x["code"] == "DV-R01" for x in L.lint(tmp_path, c, list(f)))


def implement(path: Path, rid: str) -> None:
    t = path.read_text()
    t = t.replace(f"// DV_REGION({rid}) BEGIN scaffold", f"// DV_REGION({rid}) BEGIN agent")
    path.write_text(t.replace("`DV_TODO(", "// implemented: (", 1) if rid.startswith("SBR") else t)


def test_regeneration_keeps_agent_regions_and_hand_edits(tmp_path):
    c, a = baseline()
    f = gen()
    res = S.write(tmp_path, f, {})
    rec = res["sha"]
    # the agent implements a rule (region owner agent) and the file keeps working after a regeneration with a new contract hash
    sb = tmp_path / "scoreboard" / "dut_scoreboard.svh"
    t = sb.read_text()
    start = t.index("// DV_REGION(SBR-001) BEGIN scaffold")
    end = t.index("// DV_REGION(SBR-001) END")
    sb.write_text(t[:start] + "// DV_REGION(SBR-001) BEGIN agent\n    mismatches += 0;  // my implementation\n    " + t[end:])
    c2 = c.model_copy(update={"metadata": c.metadata.model_copy(update={"contract_hash": "f" * 64})})
    r2 = S.write(tmp_path, gen(c2, a), rec)
    assert "scoreboard/dut_scoreboard.svh" in r2["written"] and r2["merged"]["scoreboard/dut_scoreboard.svh"] == ["SBR-001"]
    text = sb.read_text()
    assert "my implementation" in text and "ffffffffffff" in text  # kept region, refreshed header
    assert [i for i in W.scan(tmp_path, c) if i["id"] == "SBR-001"][0]["open"] is False
    # an edit outside a region is never overwritten
    top = tmp_path / "tb_top.sv"
    top.write_text(top.read_text() + "\n// hand-written line\n")
    r3 = S.write(tmp_path, gen(), r2["sha"])
    assert "tb_top.sv" in r3["kept_edited"] and "hand-written line" in top.read_text()
    assert (tmp_path / "tb_top.sv.scaffold_new").is_file()


def test_evidence_judges_from_the_log_only():
    good = "VLSIT_DUT_TB_RESULT test=dut_smoke_test tc=TC-001 status=PASS checks=3 mismatches=0 todo=0\nUVM_ERROR :    0\nUVM_FATAL :    0\n"
    assert ev.judge_run(good, 0, "dut_smoke_test") == ("PASS", "")
    assert ev.judge_run(good, 1, "dut_smoke_test")[0] == "FAIL"
    assert ev.judge_run(good.replace("UVM_ERROR :    0", "UVM_ERROR :    2"), 0, "dut_smoke_test")[0] == "FAIL"
    assert ev.judge_run(good.replace("PASS checks", "INCOMPLETE checks"), 0, "dut_smoke_test")[0] == "FAIL"
    assert "summary not found" in ev.judge_run("VLSIT_DUT_TB_RESULT test=t tc= status=PASS checks=1 mismatches=0 todo=0", 0, "t")[1]
    assert ev.judge_run("UVM_ERROR :    0\nUVM_FATAL :    0\n", 0, "t")[1].startswith("no VLSIT_DUT_TB_RESULT")


def test_step_writes_scaffold_worklist_validation_and_evidence(engine, llm):
    root = engine.project.root
    for n in ("golden_manifest", "verification_contract", "tb_architecture"):
        shutil.copy(EXAMPLE / "schemas" / f"{n}.json", root / "schemas")
    cfg = (root / "q3tui.yaml").read_text().replace("sim: {cmd: 'true', parse: vcs}", "sim: {cmd: 'true', parse: vcs}\n    sim_uvm: {cmd: 'true', parse: vcs}")
    (root / "q3tui.yaml").write_text(cfg)
    engine2 = type(engine)(type(engine.project).open(root))
    from tests.test_dut_dv_intent import intent_from_contract
    llm.dv_intents = [intent_from_contract(load_contract(root))]
    with pytest.raises(Exception, match="NOT_RUN|nothing was simulated"):
        run_tb(engine2)
    tb = root / "src" / "tb"
    assert (tb / "tb_top.sv").is_file() and (tb / "scoreboard" / "dut_scoreboard.svh").is_file()
    wl = json.loads((root / "schemas" / "dv_worklist.json").read_text())
    assert wl["summary"]["open"] > 20 and json.loads((root / "schemas" / "dv_validation.json").read_text())["errors"] == 0
    ev = json.loads((root / "schemas" / "dv_tb_evidence.json").read_text())["entries"]  # the tb step's two compiles: against the interface shell, then with the RTL
    assert [x["name"] for x in ev] == ["tb + VIP + interface shell (no DUT)", "tb + VIP + RTL (DUT)"]
    assert all(x["kind"] == "compile" and x["command"] == "true" and x["returncode"] == 0 and x["status"] == "PASS" for x in ev)
    assert "not a DUT result" in ev[0]["note"] and "released RTL as the DUT" in ev[1]["note"] and ev[0]["uvm_error"] is None
    mine = {str(p) for p in engine2.by_name["tb"].outputs(engine2)}
    plan = {str(p) for p in engine2.by_name["testplan"].outputs(engine2)}
    sim = {str(p) for p in engine2.by_name["simulate"].outputs(engine2)}
    assert str(tb / "tb_top.sv") in mine and str(tb / "integration" / "dut_wrapper.sv") in mine and str(root / "schemas" / "dut_integration.json") in mine
    assert str(root / "schemas" / "verification_contract.json") in plan and not (plan & mine) and not (mine & sim)  # each step has its own files


def test_no_tool_role_means_no_evidence(engine, llm):
    root = engine.project.root
    for n in ("golden_manifest", "verification_contract", "tb_architecture"):
        shutil.copy(EXAMPLE / "schemas" / f"{n}.json", root / "schemas")
    from tests.test_dut_dv_intent import intent_from_contract
    llm.dv_intents = [intent_from_contract(load_contract(root))]
    with pytest.raises(Exception, match="NOT_RUN|nothing was simulated"):
        run_tb(engine)
    assert not (root / "schemas" / "dv_evidence.json").exists()  # sim_uvm is not configured: NOT_RUN, nothing recorded


def test_widths_follow_the_names_the_specification_uses():
    """The templates never assume a parameter prefix: a contract with other names gets code with those names."""
    c, a = baseline()
    ren = json.loads(c.model_dump_json().replace("PARA_", "XP_"))
    c2 = C.VerificationContract.model_validate(ren)
    f = S.generate(c2, a, OPTS, "demo", "engineer", "2026-10-19")
    txn = f["env/dut_txn.svh"]
    assert "bit [XP_ID_WD-1:0]" in txn and "bit [XP_DATA_WD-1:0]        data[$];" in txn and "@" not in txn.replace("@(", "")
    assert not any("PARA_" in t or "@DATA_WD@" in t for t in f.values())
    with pytest.raises(ValueError, match="exactly one parameter for the role DATA_WD"):
        S.generate(c.model_copy(update={"interface_contract": c.interface_contract.model_copy(update={"parameters": [p for p in c.interface_contract.parameters if "DATA" not in p.name]})}),
                   a, OPTS, "demo", "engineer", "2026-10-19")


def test_regeneration_keeps_agent_regions_even_when_the_file_is_as_recorded(tmp_path):
    """The record can hold the hash of a file the agent already filled: a regeneration must not put the stubs back."""
    files = gen()
    S.write(tmp_path, files, {})
    rel = next(r for r, t in files.items() if "BEGIN scaffold" in t and "DV_TODO(" in t)
    import re

    text = re.sub(r"(?m)^(\s*// DV_REGION\([\w.\-]+\) BEGIN )scaffold", r"\1agent", (tmp_path / rel).read_text(), count=1)
    (tmp_path / rel).write_text(text)
    import hashlib

    res = S.write(tmp_path, files, {rel: hashlib.sha256(text.encode()).hexdigest()})
    assert (tmp_path / rel).read_text() == text and not list(tmp_path.rglob("*.scaffold_new")) and rel in res["unchanged"]


def test_an_ip_without_axi_style_widths_is_refused_with_a_reason_not_a_traceback():
    c, _ = baseline()
    assert S.unsupported(c) is None  # the AXI SRAM controller: ID / ADDR / DATA width parameters
    sorter = c.model_copy(update={"interface_contract": c.interface_contract.model_copy(update={"parameters": [
        p.model_copy(update={"name": n}) for p, n in zip(c.interface_contract.parameters, ("PARA_K", "PARA_W"))][:2]})})
    why = S.unsupported(sorter)
    assert why and "only supports IPs with an AXI-style memory-mapped interface" in why and "PARA_K, PARA_W" in why and "ID_WD" in why and "Testbench kind to plain" in why
