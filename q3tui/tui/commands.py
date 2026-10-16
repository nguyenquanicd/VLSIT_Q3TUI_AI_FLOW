"""Slash-command table for the TUI command line: completion (ghost text) and the hint line."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from textual.suggester import Suggester


@dataclass(frozen=True)
class Command:
    usage: str
    help: str
    # completion source per argument position: a kind resolved by the app ("step", "gate",
    # "question", "model", "effort", "file") or a tuple of literal values
    args: tuple[str | tuple[str, ...], ...] = ()


ON_OFF = (("on", "off"), ("--save",))

COMMANDS: dict[str, Command] = {
    "run": Command("[from] [to]", "run the pipeline (optionally a range of steps)", ("step", "step")),
    "stop": Command("", "stop the running step, else the assistant's reply (Esc also stops the reply)"),
    "approve": Command("<step>|all [--force]", "approve a review gate, or every gate waiting for review", ("gate", ("--force",))),
    "change": Command("<step> <text>", "queue a change request for a step", ("step",)),
    "answer": Command("<id> <text> | all", "answer an open question, or `all`: every open question takes its default", ("question",)),
    "gate": Command("<step> [mode] [--session]", "gate mode of a step: human | auto | auto_answer | none (no mode: pick one)",
                    ("gate", ("human", "auto", "auto_answer", "none"), ("--session",))),
    "flow": Command("", "flow editor: add / remove / move steps; deps, gate, view, pass conditions, notes (key F)"),
    "skill": Command("", "skill editor: read and edit a step's skill.md — settings, notes, stage system prompts (key K)"),
    "prompts": Command("", "prompt editor: read the built-in system prompt of a step / stage, add or replace it (key P)"),
    "step": Command("<step> view|pass|notes [text] [--session]", "customise a step: files to show, pass conditions (a ;; b), notes for its LLM tasks",
                    ("step", ("view", "pass", "notes"), ("--session",))),
    "gates": Command("", "every review gate with its mode and state"),
    "reset": Command("<step>|all [--only]", "clear a step's outputs (and the steps after it), or everything", ("step_all", ("--only",))),
    "model": Command("[id] [--save]", "switch the LLM model (no id: the settings' LLM tab)", ("model", ("--save",))),
    "effort": Command("<level>|none [--save]", "set the reasoning effort", ("effort", ("--save",))),
    "specreview": Command("on|off [--save]", "spec self-review after drafting", ON_OFF),
    "autoapprove": Command("on|off [--save]", "approve review gates automatically (blocking questions still stop)", ON_OFF),
    "autoanswer": Command("on|off [--save]", "unattended: approve gates, answer questions with defaults, accept proposals", ON_OFF),
    "autoconfirm": Command("on|off [--save]", "confirm a step's own reviewable items (SVA assertions, flagged requirements) without "
                           "asking; gates still stop for Approve", ON_OFF),
    "parallel": Command("on|off [--multi-agent|--shared] [--save]",
                        "vlsit: run rtl (lint+synth) and tb together instead of in sequence; --multi-agent: tb gets its own LLM session",
                        (("on", "off"), ("--multi-agent", "--shared", "--save"), ("--save",))),
    "status": Command("", "print every step's status"),
    "open": Command("<file>", "show a file in the step view", ("file",)),
    "cost": Command("[reset]", "show (or clear) the LLM cost", (("reset",),)),
    "settings": Command("[tab]", "settings dialog: general, gates, llm, models (per step), spec, rtl, tb",
                        (("general", "gates", "llm", "models", "spec", "rtl", "tb"),)),
    "stats": Command("[reset]", "usage statistics: tokens per model / step / stage, cost, time (reset: start over)",
                     (("reset",),)),
    "newchat": Command("", "start a new assistant conversation"),
    "help": Command("", "keys and commands"),
    "quit": Command("", "leave the TUI"),
}

Source = Callable[[str], list[str]]


def _position(value: str) -> tuple[str, list[str], str]:
    """'/approve re' → ('approve', [], 're'); '/reset spec --o' → ('reset', ['spec'], '--o')."""
    tokens = value[1:].split(" ")
    return tokens[0], tokens[1:-1], tokens[-1]


def candidates(value: str, source: Source) -> list[str]:
    """Completions for the token being typed (whole tokens, not just the missing part)."""
    if not value.startswith("/"):
        return []
    if " " not in value:
        return [f"/{name}" for name in COMMANDS if name.startswith(value[1:])]
    name, done, last = _position(value)
    cmd = COMMANDS.get(name)
    if cmd is None or len(done) >= len(cmd.args):
        return []
    kind = cmd.args[len(done)]
    options = list(kind) if isinstance(kind, tuple) else source(kind)
    return [o for o in options if o.startswith(last)]


def complete(value: str, source: Source) -> str | None:
    """The full input with the current token completed to the first candidate."""
    found = candidates(value, source)
    if not found:
        return None
    head = value[: value.rfind(" ") + 1] if " " in value else ""
    full = head + found[0]
    return full if len(full) > len(value) else None


def hint(value: str, source: Source) -> str:
    """One line shown above the command line while typing a slash command."""
    if not value.startswith("/"):
        return ""
    if " " not in value:
        names = [n for n in COMMANDS if n.startswith(value[1:])]
        if not names:
            return f"unknown command {value} · /help"
        if len(names) == 1:
            cmd = COMMANDS[names[0]]
            return f"/{names[0]} {cmd.usage} — {cmd.help}".replace("  —", " —")
        return "  ".join(f"/{n}" for n in names)
    name, done, _ = _position(value)
    cmd = COMMANDS.get(name)
    if cmd is None:
        return f"unknown command /{name} · /help"
    usage = f"/{name} {cmd.usage} — {cmd.help}"
    found = candidates(value, source)
    return f"{usage}   ▸ {' · '.join(found[:12])}{' …' if len(found) > 12 else ''}" if found else usage


class CommandSuggester(Suggester):
    """Ghost-text completion for slash commands; accept with Tab or →."""

    def __init__(self, source: Source) -> None:
        super().__init__(use_cache=False, case_sensitive=True)
        self.source = source

    async def get_suggestion(self, value: str) -> str | None:
        return complete(value, self.source)
