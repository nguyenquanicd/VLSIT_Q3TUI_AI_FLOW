"""Milestone M6: the separate RTL phase. The RTL is read only after the contract is frozen, for structure only; a mismatch is a finding."""

import json
import shutil

import pytest

from q3tui.flows.vlsit.tb.uvm import contract as C
from q3tui.flows.vlsit.tb.uvm import integrate as I
from q3tui.flows.vlsit.tb.uvm import spec_facts as sf
from tests.dutdv_rtl import EXAMPLE as EX, mini_rtl
from tests.test_dut_dv_intent import EXAMPLE, engine, intent_from_contract, llm, load_contract, run_tb  # noqa: F401  (fixtures)
from tests.test_dut_dv_scaffold import baseline


def rtl_of(tmp_path, text, name="dut.sv"):
    d = tmp_path / "rtl"
    d.mkdir(exist_ok=True)
    (d / name).write_text(text)
    return [d / name]


def contract():
    return baseline()[0]


def test_reads_only_the_interface_of_the_top(tmp_path):
    r = I.read_interface(rtl_of(tmp_path, mini_rtl()), "m_vlsi_axi4_sram", [], {}, {"PARA_DATA_WD": "32"})
    assert r["found"] and len(r["ports"]) == 37 and {p["name"] for p in r["parameters"]} >= {"PARA_DATA_WD", "PARA_NUM_BANKS"} and r["diagnostics"] == []
    assert r["ports"][2] == {"name": "i_awaddr", "direction": "in", "type": "logic[31:0]"}
    assert not I.read_interface(rtl_of(tmp_path, mini_rtl("other")), "m_vlsi_axi4_sram", [], {}, {})["found"]


def test_a_conforming_rtl_has_no_finding(tmp_path):
    c = contract()
    r = I.read_interface(rtl_of(tmp_path, mini_rtl()), "m_vlsi_axi4_sram", [], {}, {p.name: p.value for p in c.interface_contract.parameters})
    assert I.conformance(c, r, "m_vlsi_axi4_sram") == []


def test_mismatches_are_findings_never_adapted(tmp_path):
    c = contract()
    ov = {p.name: p.value for p in c.interface_contract.parameters}
    # a renamed parameter: a name the specification itself lists is only a suggestion until the user approves a mapping
    suggest = {p.name: "PR_" + p.name.split("_", 1)[1] for p in c.interface_contract.parameters}
    r = I.read_interface(rtl_of(tmp_path, mini_rtl(param_prefix="PR_")), "m_vlsi_axi4_sram", [], {}, {})
    f = I.conformance(c, r, "m_vlsi_axi4_sram", spec_names=suggest)
    assert [x["code"] for x in f if x["severity"] == "error"] == ["parameter_missing"] * 6 and "original name PR_DATA_WD" in f[0]["text"] and "integration.param_map" in f[0]["text"]
    assert I.conformance(c, r, "m_vlsi_axi4_sram", param_map=suggest) == []  # an approved mapping resolves it
    # a missing port, a direction, a width, an extra input, another top name
    for kw, code in (({"drop_port": "o_bvalid"}, "port_missing"), ({"widen": {"i_awid": "8"}}, "width")):
        r = I.read_interface(rtl_of(tmp_path, mini_rtl(**kw)), "m_vlsi_axi4_sram", [], {}, ov)
        assert code in {x["code"] for x in I.conformance(c, r, "m_vlsi_axi4_sram")}
    r = I.read_interface(rtl_of(tmp_path, mini_rtl()), "m_vlsi_axi4_sram", [], {}, ov)
    assert "top_name" in {x["code"] for x in I.conformance(c, r, "wrong_top")}
    bad = mini_rtl().replace("input logic  i_clk", "input logic  i_clk,\n  input logic  i_extra")
    r = I.read_interface(rtl_of(tmp_path, bad), "m_vlsi_axi4_sram", [], {}, ov)
    assert any(x["code"] == "port_extra" and x["severity"] == "error" for x in I.conformance(c, r, "m_vlsi_axi4_sram"))


def test_wrapper_and_filelist(tmp_path):
    c = contract()
    r = I.read_interface(rtl_of(tmp_path, mini_rtl(param_prefix="PR_")), "m_vlsi_axi4_sram", [], {}, {})
    w = I.wrapper(c, r, "m_vlsi_axi4_sram", "", {"PARA_DATA_WD": "PR_DATA_WD"})
    assert ".PR_DATA_WD(PARA_DATA_WD)" in w and ".i_awaddr(pins.i_awaddr)" in w and "u_dut" in w and "module dut_wrapper(dut_pins_if pins);" in w
    fl = I.dut_filelist("a.sv\nintegration/dut_wrapper_shell.sv\ntb_top.sv\n", ["../../rtl/dut.sv"])
    assert fl.splitlines() == ["a.sv", "../../rtl/dut.sv", "integration/dut_wrapper.sv", "tb_top.sv"]


def prepare(engine, llm):
    root = engine.project.root
    for n in ("golden_manifest", "verification_contract", "tb_architecture"):
        shutil.copy(EXAMPLE / "schemas" / f"{n}.json", root / "schemas")
    cfg = (root / "q3tui.yaml").read_text().replace("sim: {cmd: 'true', parse: vcs}", "sim: {cmd: 'true', parse: vcs}\n    sim_uvm: {cmd: 'true', parse: vcs}")
    (root / "q3tui.yaml").write_text(cfg)
    llm.dv_intents = [intent_from_contract(load_contract(root))]
    return root, type(engine)(type(engine.project).open(root))


def test_step_integrates_the_rtl_after_the_freeze(engine, llm):
    root, eng = prepare(engine, llm)
    with pytest.raises(Exception, match="NOT_RUN|nothing was simulated"):
        run_tb(eng)
    d = json.loads((root / "schemas" / "dut_integration.json").read_text())
    assert d["conformance"]["ok"] and d["rtl"]["top"] == "m_vlsi_axi4_sram" and d["rtl"]["files"] == ["src/rtl/dut.sv"] and "structure and compilation only" in d["rtl"]["read_for"]
    assert set(d["frozen_inputs"]) == {"verification_contract.json", "tb_architecture.json", "golden_manifest.json"}
    assert "integration/dut_wrapper.sv" in (root / "src" / "tb" / "filelist_dut.f").read_text() and "dut_wrapper_shell" not in (root / "src" / "tb" / "filelist_dut.f").read_text()
    ev = json.loads((root / "schemas" / "dv_evidence.json").read_text())["entries"]
    assert ev[-1]["name"] == "tb + VIP + RTL (DUT)" and ev[-1]["status"] == "PASS"
    # independence: no LLM stage of the behavioral phase or of the implementation saw the RTL
    assert all("RTL_SENTINEL_TOKEN" not in c.prompt and root / "src" / "rtl" in c.deny_dirs for c in llm.calls)
    assert "CONFORMS" in (root / "docs" / "dut_conformance_report.md").read_text()


def test_a_nonconforming_rtl_stops_with_a_report_and_changes_nothing_else(engine, llm):
    root, eng = prepare(engine, llm)
    (root / "src" / "rtl" / "dut.sv").write_text(mini_rtl(param_prefix="PR_"))
    with pytest.raises(Exception, match="does not conform") as exc:
        run_tb(eng)
    assert "integration.param_map" in str(exc.value)  # how to approve a mapping; nothing was applied
    d = json.loads((root / "schemas" / "dut_integration.json").read_text())
    assert not d["conformance"]["ok"] and d["wrapper"] is None
    assert not (root / "src" / "tb" / "integration" / "dut_wrapper.sv").exists() and "DOES NOT CONFORM" in (root / "docs" / "dut_conformance_report.md").read_text()
    from q3tui.core.project import sha256_file
    assert d["frozen_inputs"] == {n: sha256_file(root / "schemas" / n) for n in d["frozen_inputs"]}  # expected behavior is untouched by the mismatch
    # the user approves the mapping: the same run now integrates
    cfg = (root / "q3tui.yaml").read_text().replace("integration:", "integration:", 1)
    (root / "q3tui.yaml").write_text(cfg.replace("kind: axi4_avip}}", "kind: axi4_avip}, integration: {param_map: {" + ", ".join(
        f"{p.name}: PR_{p.name.split('_', 1)[1]}" for p in contract().interface_contract.parameters) + "}}}"))
    eng2 = type(eng)(type(eng.project).open(root))
    with pytest.raises(Exception, match="NOT_RUN|nothing was simulated"):
        run_tb(eng2)
    assert json.loads((root / "schemas" / "dut_integration.json").read_text())["conformance"]["ok"]


def test_integration_needs_a_frozen_contract(engine, llm):
    root, eng = prepare(engine, llm)
    from q3tui.flows.vlsit.tb.uvm import integration_phase  # noqa: F401
    c = contract().model_copy(update={"metadata": contract().metadata.model_copy(update={"status": "draft"})})
    import anyio
    from types import SimpleNamespace
    from q3tui.flows.vlsit.tb.uvm import integration_phase as ip, parse_options
    with pytest.raises(Exception, match="frozen verification contract"):
        anyio.run(lambda: ip(SimpleNamespace(lay=lambda e: None, options={}), SimpleNamespace(engine=None, project=None), parse_options({"uvm_vip": {"vip": {"path": "x"}}}), c, {}, None))


# -- the smoke simulation (milestone M8): the status comes from the log only ---------------------------------------------

FAKE_RUN = '''
import sys
test, seed, mode = sys.argv[1], sys.argv[2], sys.argv[3]
if mode == "hang":
    print("UVM_FATAL x.svh(1) @ 20: reporter [PH_TIMEOUT] Explicit timeout hit")
    print("UVM_ERROR :    0"); print("UVM_FATAL :    1"); sys.exit(0)
status = {"pass": "PASS", "todo": "INCOMPLETE"}[mode]
print(f"UVM_INFO t.svh(1) @ 0: uvm_test_top [DUT_RESULT] VLSIT_DUT_TB_RESULT test={test} tc=TC-001 status={status} checks=7 mismatches=0 todo={0 if mode == 'pass' else 2}")
print("UVM_ERROR :    0"); print("UVM_FATAL :    0")
'''


def with_run_role(root, tmp_path, mode):
    import sys
    script = tmp_path / "fake_run.py"
    script.write_text(FAKE_RUN)
    cfg = (root / "q3tui.yaml").read_text().replace("sim_uvm: {cmd: 'true', parse: vcs}", f"sim_uvm: {{cmd: 'true', parse: vcs}}\n    run_uvm: {{cmd: '{sys.executable} {script} {{test}} {{seed}} {mode}', parse: vcs}}")
    (root / "q3tui.yaml").write_text(cfg)


@pytest.mark.parametrize("mode,ok,why", [("pass", True, ""), ("todo", False, "INCOMPLETE"), ("hang", False, "UVM_FATAL=1")])
def test_smoke_status_is_judged_from_the_log(engine, llm, tmp_path, mode, ok, why):
    root, eng = prepare(engine, llm)
    with_run_role(root, tmp_path, mode)
    eng = type(eng)(type(eng.project).open(root))
    if ok:
        run_tb(eng)
    else:
        with pytest.raises(Exception, match="did not pass") as exc:
            run_tb(eng)
        assert why in str(exc.value) and "never a reason to change it" in str(exc.value)
    e = json.loads((root / "schemas" / "dv_evidence.json").read_text())["entries"][-1]
    assert e["kind"] == "run" and e["name"] == "dut_smoke_test" and e["seed"] == 1 and e["returncode"] == 0 and e["status"] == ("PASS" if ok else "FAIL")
    assert e["uvm_error"] == 0 and e["uvm_fatal"] == (0 if mode != "hang" else 1) and e["log"].endswith("run_dut_smoke_test_seed1.log")


def test_a_second_run_does_not_treat_the_integration_files_as_hand_edited(engine, llm, tmp_path):
    root, eng = prepare(engine, llm)
    with_run_role(root, tmp_path, "pass")
    eng = type(eng)(type(eng.project).open(root))
    run_tb(eng)
    wr = root / "src" / "tb" / "integration" / "dut_wrapper.sv"
    first = wr.read_text()
    run_tb(type(eng)(type(eng.project).open(root)))
    assert wr.read_text() == first and not list((root / "src" / "tb").rglob("*.scaffold_new"))
