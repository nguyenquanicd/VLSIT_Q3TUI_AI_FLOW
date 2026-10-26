"""The optional `uvm_vip` backend of the VLSIT tb step: option model, input classes, rule file, dispatch (milestone M1).
No LLM, no EDA tool."""

import re
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

from q3tui.flows.dv import uvm
from q3tui.pipeline.base import StepFailed

RULE_FILE = Path(uvm.__file__).parents[1] / "tb" / "rules" / "vlsit_dut_dv_rule.md"


def opts(**over):
    base = {"uvm_vip": {"vip": {"path": "vip/axi", "kind": "axi4_avip"}}}
    base["uvm_vip"].update(over)
    return base


def test_the_dv_flow_is_testplan_testbench_simulate_and_needs_the_option_group():
    from q3tui import flows

    flow = flows.load_flow("dv")
    assert [s.id for s in flow.steps] == ["spec", "parse", "config", "testplan", "tb", "simulate"] and flows.validate(flow) == []
    assert [(s.kind, s.deps, s.gate) for s in flow.steps[3:]] == [("dv_testplan", ["config"], "human"), ("dv_tb", ["testplan"], "auto"), ("dv_simulate", ["tb"], "auto")]
    assert uvm.input_problem({}, Path("/nonexistent")) is None  # no VIP is not a problem: only a VIP that is set must exist, and only an interface that needs one waits for it
    assert not hasattr(uvm, "backend_of")


def test_uvm_vip_options_parse_and_reject_unknown_keys():
    o = uvm.parse_options(opts(standards=["spec/axi4.md"], integration={"top": "m_x", "defines": {"A": "1"}}))
    assert o.vip.path == "vip/axi" and o.integration.top == "m_x" and o.integration.simulator == "vcs"
    assert o.integration.smoke_test == "dut_smoke_test"
    with pytest.raises(uvm.OptionsError, match="uvm_vip.integration.rtl"):
        uvm.parse_options(opts(integration={"rtl": "x"}))
    with pytest.raises(uvm.OptionsError, match="uvm_vip.semantic.scope"):
        uvm.parse_options({"scope": "everything"})


def test_input_classes_are_separated():
    assert set(uvm.INPUT_CLASSES.values()) == {"behavioral", "vip", "integration"}
    base = uvm.behavioral_fingerprint(opts())
    # integration inputs never make the behavioral phase stale ...
    assert uvm.behavioral_fingerprint(opts(integration={"rtl_dir": "other", "top": "t", "defines": {"X": "1"}, "incdirs": ["i"]})) == base
    # ... behavioral and VIP inputs do
    assert uvm.behavioral_fingerprint(opts(standards=["s.md"])) != base
    assert uvm.behavioral_fingerprint(opts(vip={"path": "vip/axi", "revision": "abc"})) != base
    assert uvm.integration_fingerprint(opts(integration={"top": "t"})) != uvm.integration_fingerprint(opts())


def test_missing_vip_blocks_the_step(tmp_path):
    msg = uvm.input_problem(opts(), tmp_path)
    assert msg and "VIP was not found" in msg and "never generates" in msg
    (tmp_path / "vip" / "axi").mkdir(parents=True)
    assert uvm.input_problem(opts(), tmp_path) is None


def test_vip_path_may_use_env_and_absolute(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_VIP", str(tmp_path / "v"))
    assert uvm.resolve_path("$MY_VIP", Path("/p")) == tmp_path / "v"
    assert uvm.resolve_path("rel/x", tmp_path) == (tmp_path / "rel" / "x").resolve()


def test_the_testbench_step_fails_cleanly_without_the_vip(tmp_path):
    from q3tui.flows.dv.tb.step import TbStep

    step = TbStep()
    step.options = opts()
    ctx = SimpleNamespace(project=SimpleNamespace(root=tmp_path))
    with pytest.raises(StepFailed, match="VIP was not found"):
        anyio.run(step.run, ctx)
    (tmp_path / "vip" / "axi").mkdir(parents=True)
    assert uvm.input_problem(step.options, tmp_path) is None  # a usable VIP: the stages of tests/test_dut_dv_intent.py take over


def test_rule_file_has_every_dut_dv_rule():
    text = RULE_FILE.read_text()
    ids = re.findall(r"^- \*\*(DUT-DV-\d{3})\*\* (MUST NOT|MUST|MAY|SHOULD):", text, re.M)
    nums = sorted(int(i[0][-3:]) for i in ids)
    assert nums == list(range(1, 32)), "DUT-DV-001..031, each once, each with a strength"
    strength = dict(ids)
    for n, s in {1: "MUST", 2: "MUST NOT", 3: "MUST NOT", 4: "MAY", 5: "MUST", 6: "MUST", 7: "MUST", 8: "MUST", 9: "MUST", 10: "MUST",
                 11: "MUST", 12: "MUST NOT", 13: "MUST", 14: "MUST", 15: "MUST"}.items():
        assert strength[f"DUT-DV-{n:03d}"] == s
    assert "SPECIFICATION / APPROVED STANDARD = GOLDEN BEHAVIOR" in text and "RTL = DUT IMPLEMENTATION ONLY" in text


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "axi4_sram_ctrl"


def test_axi4_sram_ctrl_example_config_has_the_uvm_options():
    from q3tui.core.config import load_config
    from q3tui.eda.tools import load_tools

    cfg = load_config(EXAMPLE / "q3tui.yaml", cwd=EXAMPLE)
    tb = cfg.pipeline.step_options["tb"]
    assert "uvm_vip" in tb and cfg.pipeline.flow == "dut_tb" and cfg.pipeline.session == "fresh"
    o = uvm.parse_options(tb)
    assert o.vip.kind == "axi4_avip" and o.integration.top == "m_vlsi_axi4_sram" and o.integration.rtl_dir == "src/rtl"
    tools = load_tools(cfg, EXAMPLE)
    assert {"sim", "run", "sim_uvm", "run_uvm"} <= set(tools.roles)
    assert "-ntb_opts" in tools.roles["sim_uvm"].cmd and "{test}" in tools.roles["run_uvm"].cmd


def test_vip_env_name_keeps_generated_lists_portable():
    o = uvm.parse_options(opts(vip={"path": "/abs/vip", "kind": "axi4_avip", "env": "AXI_VIP_HOME"}))
    assert o.vip.env == "AXI_VIP_HOME"
    assert uvm.parse_options(opts()).vip.env is None


# -- the options of the Settings dialog: flat on the tb step, laid over the nested group ---------------------------------------------------------------

def test_flat_options_of_the_settings_dialog_make_the_group_and_win_over_it():
    o = uvm.parse_options({"vip_path": "vip/axi", "vip_kind": "axi4_avip", "rtl_dir": "rtl", "top": "m_x", "scope": "all"})
    assert (o.vip.path, o.vip.kind, o.integration.rtl_dir, o.integration.top, o.semantic.scope) == ("vip/axi", "axi4_avip", "rtl", "m_x", "all")
    both = {"uvm_vip": {"vip": {"path": "from/yaml", "kind": "axi4_avip"}, "standards": ["s.md"]}, "vip_path": "from/dialog", "top": ""}
    o = uvm.parse_options(both)
    assert o.vip.path == "from/dialog" and o.vip.kind == "axi4_avip" and o.standards == ["s.md"] and o.integration.top is None  # (an empty value is unset)
    assert both["uvm_vip"]["vip"]["path"] == "from/yaml"  # the group is not changed
    assert uvm.input_problem({"vip_path": "nowhere"}, Path("/p")).startswith("the VIP was not found")


def test_the_vip_location_is_set_from_the_settings_dialog(tmp_path):
    from q3tui.core.ops import Ops
    from q3tui.core.project import Project
    from q3tui.pipeline.engine import Engine

    (tmp_path / "q3tui.yaml").write_text("pipeline: {flow: dv}\n")
    (tmp_path / "vip" / "axi").mkdir(parents=True)
    eng = Engine(Project.open(tmp_path))
    tabs = {s.path for s in eng.flow.settings}
    assert {"pipeline.step_options.tb.vip_path", "pipeline.step_options.tb.rtl_dir", "pipeline.step_options.tb.scope"} <= tabs
    assert uvm.input_problem(eng.by_name["tb"].options, tmp_path) is None  # no VIP set: not a problem
    Ops(eng).apply_settings({"pipeline.step_options.tb.vip_path": "vip/axi", "pipeline.step_options.tb.scope": "all"}, save=True)
    assert uvm.input_problem(eng.by_name["tb"].options, tmp_path) is None and uvm.parse_options(eng.by_name["tb"].options).semantic.scope == "all"
    assert uvm.parse_options(eng.by_name["tb"].options).vip.path == "vip/axi"
    again = Engine(Project.open(tmp_path))  # saved to q3tui.yaml: the next start has it
    assert uvm.input_problem(again.by_name["tb"].options, tmp_path) is None


# -- the VIP is optional: a simple IP has no protocol interface to drive with one --------------------------------------------------------------

def test_the_vip_is_optional_and_only_a_configured_one_is_checked(tmp_path):
    o = uvm.parse_options({})  # nothing set: the defaults, no VIP
    assert o.vip is None and o.integration.rtl_dir == "src/rtl" and o.semantic.scope == "smoke"
    assert uvm.parse_options({"uvm_vip": {"vip": {}}}).vip is None  # (a vip without a path is none)
    assert uvm.input_problem({}, tmp_path) is None  # the test plan (and a simple IP's testbench) never waits for a VIP
    assert "VIP was not found" in uvm.input_problem(opts(), tmp_path)  # a VIP that is set must be there
    assert uvm.behavioral_fingerprint({}) != uvm.behavioral_fingerprint(opts())  # having a VIP shapes the contract (its interfaces' stimulus agents)


def test_the_intent_stage_is_told_whether_there_is_a_vip():
    from q3tui.flows.dv.uvm import task as tasks
    from tests.test_dut_dv_intent import example_draft

    base = dict(contract=example_draft().model_dump(mode="json"), spec_files=["spec/spec.md"], answers={}, settled="", feedback=[], problems=[], previous=False)
    assert "`axi4_avip`: an interface it can drive as the master has the stimulus agent `vip_master`" in tasks.intent_prompt(**base, vip="axi4_avip")
    assert "None is configured: never use `vip_master`" in tasks.intent_prompt(**base)


def test_an_interface_that_needs_a_vip_waits_for_one_and_a_simple_ip_does_not(tmp_path):
    from q3tui.flows.dv.uvm import arch
    from tests.test_dut_dv_scaffold import baseline

    c, a = baseline()  # the AXI SRAM controller: its AXI interface is driven by a VIP
    assert arch.needs_vip(c) and arch.derive(c, {"vip": None, "integration": {"simulator": "vcs", "smoke_test": "t"}})["interfaces"][0]["vip_binding"] is None
    bad = arch.derive(c, {"vip": None, "integration": {"simulator": "vcs", "smoke_test": "t"}})
    assert any("no VIP is set" in p for p in arch.check(bad, c))
    simple = c.model_copy(update={"interface_contract": c.interface_contract.model_copy(update={"interfaces": [
        i.model_copy(update={"stimulus_agent": "none"}) for i in c.interface_contract.interfaces]})})
    assert arch.needs_vip(simple) == [] and not any("no VIP" in p for p in arch.check(arch.derive(simple, {"vip": None, "integration": {"simulator": "vcs", "smoke_test": "t"}}), simple))
