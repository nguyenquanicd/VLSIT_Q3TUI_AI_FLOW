"""q3tui.yaml loading and schema. See docs/spec/config.md."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


Effort = Literal["low", "medium", "high", "xhigh", "max"]


class StepLLM(_Strict):
    """Model / effort for one step's LLM stages; unset = the global llm.model / llm.effort."""

    model: str | None = None  # null = the global model
    effort: Literal["default", "none", "low", "medium", "high", "xhigh", "max"] = "default"  # none = no effort (Haiku)


class StepModels(_Strict):
    """Per-step overrides (llm.steps.<step>)."""

    spec: StepLLM = Field(default_factory=StepLLM)
    parse: StepLLM = Field(default_factory=StepLLM)
    config: StepLLM = Field(default_factory=StepLLM)
    rtl: StepLLM = Field(default_factory=StepLLM)
    tb: StepLLM = Field(default_factory=StepLLM)
    sva: StepLLM = Field(default_factory=StepLLM)
    verify: StepLLM = Field(default_factory=StepLLM)
    doc: StepLLM = Field(default_factory=StepLLM)
    assistant: StepLLM = Field(default_factory=StepLLM)


STEP_LLM_KEYS = tuple(StepModels.model_fields)


def uses_effort(model: str) -> bool:
    """Haiku 4.5 has no effort setting (its thinking is capped by llm.thinking_budget instead)."""
    return not model.startswith("claude-haiku-4")


class LLMConfig(_Strict):
    model: str = "claude-opus-5-5"
    fallback_model: str | None = None
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = "high"  # null for models without effort (Haiku 4.5)
    # models without effort think without limit by default (a 2.5-minute review on Haiku):
    # cap their extended thinking at this many tokens; 0 = no thinking; null = model default
    thinking_budget: int | None = Field(6000, ge=0)
    max_turns: int = 60
    max_budget_usd: float | None = None
    # every tool result is re-sent on every later turn: cap what one call returns (characters)
    tool_output_limit: int = Field(6000, ge=500)
    # no longer used — like Claude Code, thinking is never cut short and the effort never lowered (kept so existing
    # configs still load); llm.timeout_s bounds a stage
    runaway_thinking_tokens: int | None = None
    runaway_seconds: float | None = None
    # warn when one stage reads more than this many input tokens (cache included): a sign of a runaway loop
    stage_token_budget: int | None = Field(1_000_000, ge=10_000)
    # per-step model / effort (e.g. Sonnet for parse, Haiku for doc)
    steps: StepModels = Field(default_factory=StepModels)

    def for_stage(self, step: str | None, stage: str) -> "LLMConfig":
        """This config with the model / effort of the step that runs `stage`; the step's set value wins over the
        global llm.model / llm.effort."""
        keys = [step] if step in STEP_LLM_KEYS else []
        entries = [getattr(self.steps, k) for k in keys]
        model = next((e.model for e in entries if e.model), None) or self.model
        effort_set = next((e.effort for e in entries if e.effort != "default"), None)
        if effort_set is not None:
            effort = None if effort_set == "none" else effort_set
        elif model != self.model:  # another model, effort not set for it: what that model supports
            effort = (self.effort or "high") if uses_effort(model) else None
        else:
            effort = self.effort
        if not uses_effort(model):
            effort = None
        if model == self.model and effort == self.effort:
            return self
        return self.model_copy(update={"model": model, "effort": effort})
    # like Claude Code: no time limit per LLM stage (null); a number only if you want one
    timeout_s: int | None = None
    env: dict[str, str] = Field(default_factory=dict)


class ToolBinding(_Strict):
    adapter: str
    bin: str | None = None
    extra_args: list[str] = Field(default_factory=list)
    rules_file: str | None = None
    modules: list[str] = Field(default_factory=list)  # loaded in addition to tools.modules


class ToolRole(_Strict):
    """A tool the flows use by role (lint, synth, sim, mutate, …), described in `tools.json` / `tools.roles`: the command
    to run and how to read its log. Placeholders in `cmd`: {top} {filelist} {files} {incdirs} {workdir} {project} and any
    the step passes (docs/spec/flows.md "Tools")."""

    cmd: str
    parse: str | dict = "generic"  # vcs | dc | verilator | yosys | iverilog | generic, or {"error": [regex], "warning": [regex]}
    description: str = ""  # shown to the LLM stage that may run it
    cwd: str | None = None  # default: the step's working directory
    timeout_s: int | None = None  # default: tools.timeout_s
    env: dict[str, str] = Field(default_factory=dict)
    modules: list[str] = Field(default_factory=list)
    ok_returncodes: list[int] = Field(default_factory=lambda: [0])
    supports: list[str] = Field(default_factory=list)  # capabilities, e.g. ["sva"]; a missing one is reported as N/A


class ToolsConfig(_Strict):
    file: str | None = None  # tools.json / tools.yaml (default: <project>/tools.json|yaml, then $Q3TUI_HOME/)
    roles: dict[str, ToolRole] = Field(default_factory=dict)  # inline roles (override the file's)
    syntax: ToolBinding = ToolBinding(adapter="slang")
    simulator: ToolBinding = ToolBinding(adapter="vcs", bin="vcs")
    lint: ToolBinding = ToolBinding(adapter="spyglass", bin="sg_shell")
    debug: ToolBinding = ToolBinding(adapter="verdi", bin="verdi")
    timeout_s: int = 600
    setup_script: str | None = None
    modules: list[str] = Field(default_factory=list)  # `module load` before every tool
    modules_init: str | None = None  # default: $MODULESHOME/init/bash


class ProjectConfig(_Strict):
    spec_dir: str = "spec"
    req_dir: str = "req"
    arch_dir: str = "arch"
    model_dir: str = "model"
    rtl_dir: str = "rtl"
    tb_dir: str = "tb"
    sim_dir: str = "sim"
    state_dir: str = ".q3tui"
    # the VLSIT flow's layout (flows/vlsit/): JSON artifacts, src/{rtl,sva,tb}, generated documents
    schemas_dir: str = "schemas"
    src_dir: str = "src"
    docs_dir: str = "docs"


class PipelineConfig(_Strict):
    # Steps with a review gate. Unset: the flow's own (flows/*.yaml `gate:`); set: replaces them (step ids of the flow).
    gates: list[str] = Field(default_factory=list)
    auto_approve: bool = False  # approve review gates automatically (stops for blocking questions)
    # unattended: approve every gate, answer every question with its default and accept proposals
    auto_answer: bool = False
    max_fix_iterations: int = Field(5, ge=1, le=20)
    escalate_after_repeats: int = Field(2, ge=1, le=10)
    # A flow name (built-in `vlsit`, or a file in <project>/flows/ or ~/.q3tui/flows/) or a path to a flow file
    # (docs/spec/flows.md).
    flow: str = "vlsit"
    # LLM sessions: null = the flow's (`session:` in the flow file); "flow" = one persistent session for the whole flow
    # (like one Claude Code conversation), "fresh" = a session per stage
    session: Literal["flow", "fresh"] | None = None
    # per-step options on top of the flow file's (`options:` of the step): {sva: {auto_confirm: true}, verify: {max_fix_rounds: 12}}
    step_options: dict[str, dict] = Field(default_factory=dict)
    # per-step overrides of the flow file's `view` / `pass` / `notes`: {rtl: {pass: ["exists rtl/*.sv"], notes: "..."}} (TUI: /step)
    step_flow: dict[str, dict] = Field(default_factory=dict)
    # per-step gate mode overrides: human | auto | auto_answer | none (flow files set the defaults; TUI: /gate)
    gate_modes: dict[str, Literal["human", "auto", "auto_answer", "none"]] = Field(default_factory=dict)
    # vlsit flow: rtl (lint + synthesis) and tb (testbench generator) read only spec/parse/config, neither
    # depends on the other's output — run them together instead of one after the other (TUI: /parallel, g)
    parallel_rtl_tb: bool = False
    # parallel_rtl_tb only: give tb its own persistent LLM session (role "verifier") instead of sharing the
    # flow's one session with rtl — true concurrent agents, not one session forked while it is busy
    parallel_multi_agent: bool = False
    # confirm a step's own reviewable items (SVA assertions, flagged requirements, …) without asking — a human
    # gate still stops for Approve, but Approve itself no longer refuses for unconfirmed items (engine.approve,
    # StepDef.pending_reviews). Narrower than auto_approve/auto_answer (TUI Settings → General); a per-step
    # `options: {sva: {auto_confirm: true}}` still overrides this one step.
    auto_confirm_reviews: bool = False


class SpecConfig(_Strict):
    self_review: bool = True  # second LLM pass that critiques and revises the draft


class RTLConfig(_Strict):
    max_fix_attempts: int = Field(3, ge=0, le=10)  # extra rounds when Q3TUI's own checks (lint, synthesis) still fail
    parallel: int = Field(4, ge=1, le=16)  # modules (RTL, SVA) written at once
    max_turns: int = Field(60, ge=5, le=400)  # agent turns per module (write, compile, fix)


class TBConfig(_Strict):
    max_fix_attempts: int = Field(3, ge=0, le=10)  # extra rounds when the plan / TB checks still fail
    max_turns: int = Field(30, ge=5, le=400)  # agent turns per TB stage
    tests_per_agent: int = Field(6, ge=1, le=100)  # test cases per agent (agents run in parallel)
    parallel: int = Field(4, ge=1, le=32)  # test-case agents at once


class TeamConfig(_Strict):
    """M10 (docs/spec/team.md): every role's tasks go to that role's one persistent session."""

    enabled: bool = False
    # markdown sections at least this long are sent once per session, then referenced (0 = never deduped)
    dedupe_chars: int = Field(300, ge=0)
    # unit-level work (a module's RTL, a block's model, a unit's plan / testbench): own = its own small session (resumed
    # for its fixes); role = the role's one session (grew to hundreds of k tokens on rv32im)
    unit_sessions: Literal["own", "role"] = "own"
    # parallel tasks of a role (independent units of one level) fork its session at its last idle point (else they
    # queue for it: 17 rv32im units' RTL written one after another)
    fork: bool = True
    # like Claude Code: a role's session compacts itself when its context fills up (the brief is re-sent after it).
    # Set a token count to compact earlier, after the task that passed it (null = never — Claude's own auto-compaction)
    max_context: int | None = Field(None, ge=10_000)


class TuiConfig(_Strict):
    icons: Literal["unicode", "nerd", "ascii"] = "unicode"  # nerd: for "… Nerd Font Mono" fonts; ascii: any font
    # the TUI's own chrome (legend, result banners, …) only — never the LLM-generated content (spec, RTL, docs, chat),
    # which stays whatever language its prompt uses (q3tui.tui.i18n)
    language: Literal["en", "vi", "ko"] = "en"


class McpServerEntry(_Strict):
    name: str
    type: Literal["http", "sse", "stdio"]
    url: str | None = None
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)


class Config(_Strict):
    llm: LLMConfig = LLMConfig()
    project: ProjectConfig = ProjectConfig()
    pipeline: PipelineConfig = PipelineConfig()
    spec: SpecConfig = SpecConfig()
    rtl: RTLConfig = RTLConfig()
    tb: TBConfig = TBConfig()
    team: TeamConfig = TeamConfig()
    tools: ToolsConfig = ToolsConfig()
    tui: TuiConfig = TuiConfig()
    mcp_servers: list[McpServerEntry] = Field(default_factory=list)

    # Where this config came from; not part of the file schema.
    source: str | None = Field(default=None, exclude=True)


CONFIG_FILENAME = "q3tui.yaml"


def q3tui_home() -> Path:
    return Path(os.environ.get("Q3TUI_HOME", "~/.q3tui")).expanduser()


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return os.path.expandvars(os.path.expanduser(value)) if ("$" in value or value.startswith("~")) else value
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def config_layers(explicit: str | Path | None = None, cwd: Path | None = None) -> list[Path]:
    """Config files merged in order (later wins): user file, project file, then --config / $Q3TUI_CONFIG."""
    layers: list[Path] = []
    for candidate in (q3tui_home() / CONFIG_FILENAME, (cwd or Path.cwd()) / CONFIG_FILENAME):
        if candidate.is_file() and candidate.resolve() not in [l.resolve() for l in layers]:
            layers.append(candidate)
    override = explicit or os.environ.get("Q3TUI_CONFIG")
    if override:
        path = Path(override).expanduser()
        if not path.is_file():
            source = "--config" if explicit else "$Q3TUI_CONFIG"
            raise FileNotFoundError(f"{source} file not found: {path}")
        if path.resolve() not in [l.resolve() for l in layers]:
            layers.append(path)
    return layers


def find_config(explicit: str | Path | None = None, cwd: Path | None = None) -> Path | None:
    """The highest-priority config file (for display); see config_layers for merging."""
    layers = config_layers(explicit, cwd)
    return layers[-1] if layers else None


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


# settings of the removed `units` / `steps` flows: still in older config files, ignored
_REMOVED = {None: ("requirements", "req", "arch", "model", "sim", "triage", "units"),
            "project": ("requirements_dir", "signoff_dir"),
            "rtl": ("rules", "review", "use_vcs", "language", "lint"),
            "tb": ("checks_per_agent", "stimulus_chars_per_agent", "session_max_turns", "use_vcs", "timeout_cycles", "seed")}
_REMOVED_STEP_MODELS = ("req", "arch", "model", "vplan", "tb_checker", "tb_stimulus", "triage", "triage_investigator")


def _legacy(layer: dict) -> dict:
    """Older config files: the settings of the removed flows are dropped (the schema rejects unknown keys)."""
    layer = {k: v for k, v in layer.items() if k not in _REMOVED[None]}
    for section, keys in _REMOVED.items():
        if section and isinstance(layer.get(section), dict):
            layer[section] = {k: v for k, v in layer[section].items() if k not in keys}
    steps = (layer.get("llm") or {}).get("steps") if isinstance(layer.get("llm"), dict) else None
    if isinstance(steps, dict):
        layer["llm"] = {**layer["llm"], "steps": {k: v for k, v in steps.items() if k not in _REMOVED_STEP_MODELS}}
    return layer


def load_config(explicit: str | Path | None = None, cwd: Path | None = None, overrides: dict | None = None) -> Config:
    """Defaults <- ~/.q3tui/q3tui.yaml <- <project>/q3tui.yaml <- --config <- overrides (e.g. --model)."""
    data: dict = {}
    layers = config_layers(explicit, cwd)
    for path in layers:
        layer = yaml.safe_load(path.read_text()) or {}
        if not isinstance(layer, dict):
            raise ValueError(f"{path}: top level must be a mapping")
        data = _merge(data, _legacy(layer))
    if overrides:
        data = _merge(data, overrides)
    try:
        cfg = Config.model_validate(_expand(data))
    except ValueError as exc:
        raise ValueError(f"invalid config ({' + '.join(map(str, layers)) or 'defaults'}): {exc}") from exc
    names = [str(p) for p in layers] + (["command line"] if overrides else [])
    cfg.source = " + ".join(names) if names else "<defaults>"
    return cfg
