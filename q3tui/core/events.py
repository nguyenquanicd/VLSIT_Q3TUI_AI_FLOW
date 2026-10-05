"""Typed event bus shared by the engine, CLI printer, TUI and the events.jsonl log."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

# Kinds rendered by front ends. `sdk` (raw Agent-SDK messages) only goes to the log file.
KINDS = (
    "run_started", "run_finished",
    "step_started", "step_finished", "step_skipped",
    "stage",
    "llm_text", "tool_call", "llm_done",
    "gate_opened", "gate_resolved",
    "questions",
    "log", "warning", "error",
    "progress",
    "sdk",
)
EPHEMERAL = {"progress"}  # delivered to subscribers, not written to events.jsonl


@dataclass
class Event:
    kind: str
    step: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    ts: datetime = field(default_factory=datetime.now)

    def to_json(self) -> str:
        return json.dumps({"ts": self.ts.isoformat(timespec="milliseconds"), "kind": self.kind, "step": self.step, **self.data}, default=str)


Subscriber = Callable[[Event], None]


class EventBus:
    def __init__(self) -> None:
        self._subs: list[Subscriber] = []
        self._log: Path | None = None
        self.cost_usd = 0.0
        self.stats_recorder: Callable[[Event], None] | None = None  # installed by the engine (q3tui.core.stats)

    def subscribe(self, fn: Subscriber) -> Callable[[], None]:
        self._subs.append(fn)
        return lambda: self._subs.remove(fn)

    def log_to(self, path: Path | None) -> None:
        self._log = path
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, kind: str, step: str | None = None, **data: Any) -> Event:
        event = Event(kind, step, data)
        if self._log and kind not in EPHEMERAL:
            with self._log.open("a") as fh:
                fh.write(event.to_json() + "\n")
        for fn in list(self._subs):
            fn(event)
        return event

    def add_cost(self, usd: float) -> None:
        self.cost_usd += usd

    def scoped(self, step: str) -> "StepEmitter":
        return StepEmitter(self, step)


@dataclass
class StepEmitter:
    bus: EventBus
    step: str

    def __call__(self, kind: str, **data: Any) -> Event:
        return self.bus.emit(kind, self.step, **data)

    def add_cost(self, usd: float) -> None:
        self.bus.add_cost(usd)
