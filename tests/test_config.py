import pytest
from pydantic import ValidationError

from q3tui.core.config import find_config, load_config


def test_defaults_when_no_file(tmp_path):
    cfg = load_config(cwd=tmp_path)
    assert cfg.source == "<defaults>"
    assert cfg.llm.model == "claude-opus-5-5"
    assert cfg.tools.simulator.adapter == "vcs"


def test_layers_merge(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "q3tui.yaml").write_text("llm: {effort: low}\ntools:\n  simulator: {adapter: vcs, modules: [synopsys/vcs]}\n")
    assert load_config(cwd=tmp_path).llm.effort == "low"

    # project file only changes the model; user-level tool modules survive
    (tmp_path / "q3tui.yaml").write_text("llm: {model: claude-sonnet-5-5}\n")
    cfg = load_config(cwd=tmp_path)
    assert (cfg.llm.model, cfg.llm.effort) == ("claude-sonnet-5-5", "low")
    assert cfg.tools.simulator.modules == ["synopsys/vcs"]

    env_cfg = tmp_path / "env.yaml"
    env_cfg.write_text("llm: {effort: max}\n")
    monkeypatch.setenv("Q3TUI_CONFIG", str(env_cfg))
    assert load_config(cwd=tmp_path).llm.effort == "max"

    explicit = tmp_path / "explicit.yaml"
    explicit.write_text("llm: {effort: xhigh}\n")
    cfg = load_config(explicit, cwd=tmp_path, overrides={"llm": {"model": "claude-haiku-4-5", "effort": None}})
    assert (cfg.llm.model, cfg.llm.effort) == ("claude-haiku-4-5", None)
    assert cfg.tools.simulator.modules == ["synopsys/vcs"]
    assert cfg.source.endswith("explicit.yaml + command line")


def test_unknown_keys_rejected(tmp_path):
    (tmp_path / "q3tui.yaml").write_text("llm: {modle: x}\n")
    with pytest.raises((ValidationError, ValueError)):
        load_config(cwd=tmp_path)


def test_env_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("SNPS", "/tools/snps")
    (tmp_path / "q3tui.yaml").write_text("tools: {setup_script: $SNPS/setup.sh}\n")
    assert load_config(cwd=tmp_path).tools.setup_script == "/tools/snps/setup.sh"


def test_missing_explicit_config(tmp_path):
    with pytest.raises(FileNotFoundError):
        find_config(tmp_path / "nope.yaml")


def test_settings_of_the_removed_flows_are_ignored(tmp_path):
    (tmp_path / "q3tui.yaml").write_text(
        "requirements: {self_review: false}\nreq: {parallel: 2}\narch: {max_repair_attempts: 1}\nmodel: {parallel: 2}\n"
        "sim: {debug_runs: 3}\ntriage: {investigate: false}\nunits: {judge: triage}\nproject: {signoff_dir: so, rtl_dir: hw}\n"
        "rtl: {rules: [a], review: false, parallel: 2}\ntb: {seed: 3, max_turns: 9}\n"
        "llm: {steps: {tb_stimulus: {model: claude-haiku-4-5}, rtl: {effort: low}}}\n")
    cfg = load_config(cwd=tmp_path)
    assert cfg.rtl.parallel == 2 and cfg.tb.max_turns == 9 and cfg.project.rtl_dir == "hw"
    assert cfg.llm.steps.rtl.effort == "low" and not hasattr(cfg, "units")
