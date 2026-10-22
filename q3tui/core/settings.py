"""User-facing settings: one registry for the TUI settings dialog, the assistant and `ops`.

Each setting is a dotted path into `Config` (e.g. "llm.effort"). Values are validated
against the real config schema before they are applied (see ops.apply_settings).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MODELS = [
    ("claude-opus-5-5", "Opus 5.5 — best quality"),
    ("claude-sonnet-5-5", "Sonnet 5.5 — fast, cheaper (default)"),
    ("claude-haiku-5-5", "Haiku 5.5 — cheap and fast; thinks at length before it answers"),
    ("claude-haiku-4-5", "Haiku 4.5 — cheapest, for trying the flow (no effort setting)"),
    ("claude-fable-5-1", "Fable 5.1 — most capable, most expensive"),
]
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
GATES = ["spec", "parse", "config", "rtl", "tb"]
STEP_LABELS = (("spec", "Spec"), ("parse", "Parse"), ("config", "Config"), ("rtl", "RTL"), ("tb", "TB"), ("assistant", "Assistant"))


@dataclass(frozen=True)
class Setting:
    path: str
    label: str
    kind: str  # bool | int | float | text | lines | choice | multi
    help: str = ""
    choices: tuple = ()  # (value, label) for choice / multi
    nullable: bool = False  # empty / "none" = null
    none_label: str = "none"  # how null is shown in the dialog (e.g. "default")
    hidden: bool = False  # not in the dialog (the assistant and ops still have it)
    default: Any = None  # a step option of a step this flow does not have (another flow's): what it is when q3tui.yaml sets nothing


@dataclass(frozen=True)
class Tab:
    id: str
    title: str
    settings: tuple[Setting, ...] = field(default_factory=tuple)


def step_model_settings(key: str, label: str) -> tuple[Setting, Setting]:
    return (Setting(f"llm.steps.{key}.model", f"{label}: model", "choice",
                    "" if key != "spec" else "Unset = the model of the LLM tab. Each step can use its own model, e.g. Sonnet "
                    "where precision matters, Haiku for mechanical work.",
                    tuple(MODELS), nullable=True, none_label="default (LLM tab)"),
            Setting(f"llm.steps.{key}.effort", f"{label}: effort", "choice", "",
                    (("default", "default"), ("none", "none (Haiku)"), *((e, e) for e in EFFORTS))))


TABS: tuple[Tab, ...] = (
    Tab("general", "General", (
        Setting("pipeline.auto_approve", "Auto-approve reviews", "bool",
                "Review gates approve themselves; a gate with blocking questions still stops and asks."),
        Setting("pipeline.auto_answer", "Auto-answer (unattended)", "bool",
                "Run start to finish without stopping: every gate approves itself, every question (blocking too) takes "
                "its default, and proposals are accepted as suggested."),
        Setting("pipeline.gates", "Review gates", "multi", "Steps that stop for your review before the next step runs "
                "(the Gates tab sets the mode of each: human, auto, auto-answer, none).",
                tuple((g, g) for g in GATES), hidden=True),
        Setting("pipeline.max_fix_iterations", "Fix loop iterations", "int",
                "How many times verify → fixes → verify may repeat before stopping."),
        Setting("pipeline.escalate_after_repeats", "Escalate after", "int",
                "A bug classified the same way this many times is handed to you instead of fixed again."),
        Setting("pipeline.auto_confirm_reviews", "Auto-confirm reviews", "bool",
                "Confirm a step's own reviewable items (flagged requirements, changed parameters, …) without asking. "
                "A human gate still stops for Approve; Approve itself no longer refuses for unconfirmed items. "
                "Narrower than Auto-approve / Auto-answer above — gates still stop, only per-item review is skipped."),
        Setting("tui.icons", "Icons", "choice",
                "Status icons: unicode (✔ ⚑ ◐ …), nerd (for a \"… Nerd Font Mono\" font), ascii (any font).",
                (("unicode", "unicode  ✔ ⚑ ◐ ★"), ("nerd", "nerd font  \uf00c \uf024 \uf110 \uf005"), ("ascii", "ascii  + # ~ *"))),
        Setting("tui.language", "Language", "choice",
                "The TUI's own chrome (legend, result banners) — never LLM-generated content (spec, RTL, docs, chat), "
                "which stays whatever language its prompt uses.",
                (("en", "English"), ("vi", "Tiếng Việt"), ("ko", "한국어"))),
    )),
    Tab("llm", "LLM", (
        Setting("llm.model", "Model", "choice", "Used by every LLM stage from the next stage on.", tuple(MODELS)),
        Setting("llm.effort", "Effort", "choice", "How hard the model thinks (Opus/Sonnet/Fable). None for Haiku 4.5.",
                tuple((e, e) for e in EFFORTS), nullable=True),
        Setting("llm.thinking_budget", "Thinking budget (tokens)", "int",
                "Only for models without effort (Haiku 4.5): caps thinking. 0 = off, empty = model default.", nullable=True),
        Setting("llm.max_budget_usd", "Max $ per stage", "float", "Stop a stage that costs more (empty = no limit).", nullable=True),
        Setting("llm.tool_output_limit", "Tool output limit (chars)", "int",
                "What one tool call may return; results are re-sent every later turn, so long ones are cut (head + tail)."),
        Setting("llm.stage_token_budget", "Token warning per stage", "int",
                "Warn when one stage reads more input tokens than this (cache included; empty = never).", nullable=True),
        Setting("llm.timeout_s", "Stage timeout (s)", "int", "Wall-clock limit per LLM stage (empty: none, like Claude Code).",
                nullable=True, none_label="no limit"),
        Setting("llm.fallback_model", "Fallback model", "text", "Used when the model is overloaded (empty = none).", nullable=True),
    )),
    Tab("spec", "Spec", (
        Setting("spec.self_review", "Self-review", "bool",
                "A second pass that checks the draft for ambiguities and returns only its changes. Off = faster drafts."),
    )),
    Tab("rtl", "RTL", (
        Setting("rtl.parallel", "Modules in parallel", "int", "Modules written at once."),
        Setting("rtl.max_fix_attempts", "Fix rounds", "int", "Extra rounds when lint or synthesis still fail."),
        Setting("rtl.max_turns", "Turns per module", "int", "Agent turns for one module (write, compile, fix)."),
    )),
    Tab("tb", "TB", (
        Setting("tb.tests_per_agent", "Tests per agent", "int", "Test cases written by one agent; agents run in parallel."),
        Setting("tb.parallel", "Agents at once", "int", "How many test-case agents run in parallel."),
        Setting("tb.max_fix_attempts", "Fix rounds", "int", "Extra rounds when the plan or TB checks still fail."),
        Setting("tb.max_turns", "Turns per stage", "int", "Agent turns for one TB stage."),
    )),
    Tab("models", "Models per step", tuple(
        s for key, label in STEP_LABELS for s in step_model_settings(key, label))),
)

_STEP_TABS = ("spec", "rtl", "tb")  # per-step settings tabs (config sections), shown when the flow has that step


STEP_OPTIONS = "pipeline.step_options."  # + <step>.<key>: a step's option, shown by a flow's add-on (addons.AddonAPI.setting)


def is_step_option(path: str) -> bool:
    return path.startswith(STEP_OPTIONS) and path.count(".") == 3


def tabs_for(steps: list[tuple[str, str]] | None = None, extra=()) -> tuple[Tab, ...]:
    """The dialog's tabs for a flow whose steps are `(id, label)`; None = the spec flow's steps. `extra`: the flow's step options."""
    rows = list(steps) if steps is not None else [(k, l) for k, l in STEP_LABELS if k != "assistant"]
    have = {k for k, _ in rows}
    keep = [t for t in TABS if t.id != "models" and (t.id not in _STEP_TABS or t.id in have)]
    for step in dict.fromkeys(x.path.split(".")[2] for x in extra):  # a flow's step options: the step's own tab
        keep.append(Tab(step, step, tuple(x for x in extra if x.path.split(".")[2] == step)))
    models = Tab("models", "Models per step", tuple(x for k, l in [*rows, ("assistant", "Assistant")] for x in step_model_settings(k, l)))
    return (*keep, models)


def find(path: str, extra=()) -> Setting | None:
    """The setting at `path`; llm.steps.<any step>.model|effort exist for every step of every flow; `extra`: the flow's step options."""
    if path in BY_PATH:
        return BY_PATH[path]
    for x in extra:
        if x.path == path:
            return x
    parts = path.split(".")
    if len(parts) == 4 and parts[:2] == ["llm", "steps"] and parts[3] in ("model", "effort"):
        return step_model_settings(parts[2], parts[2])[0 if parts[3] == "model" else 1]
    return None


BY_PATH = {s.path: s for t in TABS for s in t.settings}
GATE_MODES = (("human", "human — stops for your review"), ("auto", "auto — approves itself (blocking questions still stop it)"),
              ("auto_answer", "auto-answer — unattended: approves, questions take their defaults"), ("none", "none — no review gate"))


def get_value(cfg, path: str) -> Any:
    obj = cfg
    for part in path.split("."):
        if isinstance(obj, dict) or obj is None:  # pipeline.step_options.<step>.<key>: absent = the flow file's value (see Ops.settings)
            obj = None if obj is None else obj.get(part)
            continue
        if not hasattr(obj, part) and path.startswith("llm.steps."):  # a step of another flow, no override set yet
            from q3tui.core.config import StepLLM

            obj = StepLLM()
            continue
        obj = getattr(obj, part)
    return obj


def current(cfg, tabs: tuple[Tab, ...] | None = None) -> dict[str, Any]:
    return {s.path: get_value(cfg, s.path) for t in (tabs or TABS) for s in t.settings}


def coerce(setting: Setting, raw: Any) -> Any:
    """A value from a form field or the assistant → the config's type (raises ValueError)."""
    if raw is None or (isinstance(raw, str) and raw.strip().lower() in ("", "none", "null")):
        if setting.nullable:
            return None
        if setting.kind in ("multi", "lines"):
            return []
        raise ValueError(f"{setting.label} needs a value")
    if setting.kind == "bool":
        if isinstance(raw, bool):
            return raw
        s = str(raw).strip().lower()
        if s in ("on", "true", "yes", "1"):
            return True
        if s in ("off", "false", "no", "0"):
            return False
        raise ValueError(f"{setting.label}: expected on/off, got {raw!r}")
    if setting.kind in ("int", "float"):
        try:
            return int(str(raw).strip()) if setting.kind == "int" else float(str(raw).strip())
        except ValueError:
            raise ValueError(f"{setting.label}: expected a {'whole ' if setting.kind == 'int' else ''}number, got {raw!r}") from None
    if setting.kind == "lines":
        return [x.strip() for x in (raw if isinstance(raw, list) else str(raw).splitlines()) if str(x).strip()]
    if setting.kind == "multi":
        items = raw if isinstance(raw, list) else [x.strip() for x in str(raw).split(",") if x.strip()]
        allowed = [v for v, _ in setting.choices]
        bad = [x for x in items if x not in allowed]
        if bad:
            raise ValueError(f"{setting.label}: unknown {', '.join(bad)} (allowed: {', '.join(allowed)})")
        return [v for v in allowed if v in items]
    if setting.kind == "choice" and setting.choices and is_step_option(setting.path):  # (a flow's option has a fixed set; model names are free)
        allowed = [v for v, _ in setting.choices]
        if str(raw).strip() not in allowed:
            raise ValueError(f"{setting.label}: {raw!r} is not one of {', '.join(allowed)}")
    return str(raw).strip()


def nested(changes: dict[str, Any]) -> dict:
    """{"llm.effort": "low"} → {"llm": {"effort": "low"}} (for q3tui.yaml)."""
    out: dict = {}
    for path, value in changes.items():
        d = out
        *parents, leaf = path.split(".")
        for p in parents:
            d = d.setdefault(p, {})
        d[leaf] = value
    return out
