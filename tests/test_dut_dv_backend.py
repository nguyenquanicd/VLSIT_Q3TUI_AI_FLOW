"""The optional `uvm_vip` backend of the VLSIT tb step: option model, input classes, rule file, dispatch (milestone M1).
No LLM, no EDA tool."""

import re
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

from q3tui.flows.dv.tb import uvm
from q3tui.flows.dv.tb.step import TbGenStep
from q3tui.pipeline.base import StepFailed

RULE_FILE = Path(uvm.__file__).parents[1] / "rules" / "vlsit_dut_dv_rule.md"


def opts(**over):
    base = {"backend": "uvm_vip", "uvm_vip": {"vip": {"path": "vip/axi", "kind": "axi4_avip"}}}
    base["uvm_vip"].update(over)
    return base


def test_plain_sv_is_the_default():
    assert uvm.backend_of(None) == "plain_sv"
    assert uvm.backend_of({}) == "plain_sv"
    assert uvm.backend_of({"backend": "plain_sv"}) == "plain_sv"
    assert uvm.input_problem({}, Path("/nonexistent")) is None


def test_unknown_backend_is_named():
    with pytest.raises(uvm.OptionsError, match="backend: 'vmm'"):
        uvm.backend_of({"backend": "vmm"})


def test_uvm_vip_options_parse_and_reject_unknown_keys():
    o = uvm.parse_options(opts(standards=["spec/axi4.md"], integration={"top": "m_x", "defines": {"A": "1"}}))
    assert o.vip.path == "vip/axi" and o.integration.top == "m_x" and o.integration.simulator == "vcs"
    assert o.integration.smoke_test == "dut_smoke_test"
    with pytest.raises(uvm.OptionsError, match="uvm_vip.vip.path"):
        uvm.parse_options({"backend": "uvm_vip", "uvm_vip": {"vip": {}}})
    with pytest.raises(uvm.OptionsError, match="uvm_vip.integration.rtl"):
        uvm.parse_options(opts(integration={"rtl": "x"}))
    with pytest.raises(uvm.OptionsError, match="uvm_vip"):
        uvm.parse_options({"backend": "uvm_vip"})


def test_input_classes_are_separated():
    assert set(uvm.INPUT_CLASSES.values()) == {"behavioral", "vip", "integration"}
    base = uvm.behavioral_fingerprint(opts())
    # integration inputs never make the behavioral phase stale ...
    assert uvm.behavioral_fingerprint(opts(integration={"rtl_dir": "other", "top": "t", "defines": {"X": "1"}, "incdirs": ["i"]})) == base
    # ... behavioral and VIP inputs do
    assert uvm.behavioral_fingerprint(opts(standards=["s.md"])) != base
    assert uvm.behavioral_fingerprint(opts(vip={"path": "vip/axi", "revision": "abc"})) != base
    assert uvm.integration_fingerprint(opts(integration={"top": "t"})) != uvm.integration_fingerprint(opts())
    assert uvm.behavioral_fingerprint({}) == uvm.behavioral_fingerprint({"backend": "plain_sv"})


def test_missing_vip_blocks_the_step(tmp_path):
    msg = uvm.input_problem(opts(), tmp_path)
    assert msg and "VIP was not found" in msg and "never generates" in msg
    (tmp_path / "vip" / "axi").mkdir(parents=True)
    assert uvm.input_problem(opts(), tmp_path) is None


def test_vip_path_may_use_env_and_absolute(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_VIP", str(tmp_path / "v"))
    assert uvm.resolve_path("$MY_VIP", Path("/p")) == tmp_path / "v"
    assert uvm.resolve_path("rel/x", tmp_path) == (tmp_path / "rel" / "x").resolve()


def test_step_dispatches_to_the_backend_and_fails_cleanly(tmp_path):
    step = TbGenStep()
    step.options = opts()
    ctx = SimpleNamespace(project=SimpleNamespace(root=tmp_path))
    with pytest.raises(StepFailed, match="VIP was not found"):
        anyio.run(step.run, ctx)
    (tmp_path / "vip" / "axi").mkdir(parents=True)
    assert uvm.input_problem(step.options, tmp_path) is None  # a usable VIP: the stages of tests/test_dut_dv_intent.py take over
    assert TbGenStep().options == {}  # the plain-SV step has no option


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


def test_axi4_sram_ctrl_example_config_selects_the_backend():
    from q3tui.core.config import load_config
    from q3tui.eda.tools import load_tools

    cfg = load_config(EXAMPLE / "q3tui.yaml", cwd=EXAMPLE)
    tb = cfg.pipeline.step_options["tb"]
    assert uvm.backend_of(tb) == "uvm_vip" and cfg.pipeline.session == "fresh"
    o = uvm.parse_options(tb)
    assert o.vip.kind == "axi4_avip" and o.integration.top == "m_vlsi_axi4_sram" and o.integration.rtl_dir == "src/rtl"
    tools = load_tools(cfg, EXAMPLE)
    assert {"sim", "run", "sim_uvm", "run_uvm"} <= set(tools.roles)
    assert "-ntb_opts" in tools.roles["sim_uvm"].cmd and "{test}" in tools.roles["run_uvm"].cmd


def test_vip_env_name_keeps_generated_lists_portable():
    o = uvm.parse_options(opts(vip={"path": "/abs/vip", "kind": "axi4_avip", "env": "AXI_VIP_HOME"}))
    assert o.vip.env == "AXI_VIP_HOME"
    assert uvm.parse_options(opts()).vip.env is None
