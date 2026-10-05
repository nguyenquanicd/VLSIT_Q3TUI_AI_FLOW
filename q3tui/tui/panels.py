"""Step panels by step kind: a flow brings its own steps, the TUI needs no code for them (docs/spec/flows.md "Panels").

`register_panel(kind, builder)`: `builder(step) -> StepPanel` for the steps of that kind (`StepDef.kind`). The spec panel
and the VLSIT flow's panels (tui/vlsit_panels.py) are registered here; a kind without a panel gets `DefaultPanel`: the
step's files, its Questions and a Log of what it did.
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import ComposeResult
from textual.widgets import RichLog, TabbedContent, TabPane

from q3tui.tui.views import FileViewer, QuestionsTable, ResultBanner, StepPanel

if TYPE_CHECKING:
    from q3tui.pipeline.base import StepDef

Builder = Callable[["StepDef"], StepPanel]
PANELS: dict[str, Builder] = {}

LOG_LINES = 300  # per step, kept by the app; shown on the default panel's Log tab


def register_panel(kind: str, builder: Builder) -> None:
    PANELS[kind] = builder


def safe_id(name: str) -> str:
    """A step id as a widget id fragment."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)


class StepLog(RichLog):
    """What a step did (its events), for panels without a tuned view."""

    def __init__(self, step: str, **kw):
        super().__init__(wrap=True, markup=False, highlight=False, max_lines=LOG_LINES, **kw)
        self.step = step
        self._seen = 0

    def refresh_view(self) -> None:
        lines = self.app.step_log.get(self.step)  # type: ignore[attr-defined]
        if lines is None:
            return
        total = getattr(lines, "total", len(lines))
        if total == self._seen:
            return
        self.clear()
        for ts, style, msg in lines:
            self.write(Text.assemble((f"{ts} ", "grey42"), (msg, style)))
        self._seen = total


class StepLines(deque):
    """A bounded list of log lines that also counts what was ever appended (to tell when something new came)."""

    def __init__(self):
        super().__init__(maxlen=LOG_LINES)
        self.total = 0

    def append(self, item) -> None:  # noqa: D401
        self.total += 1
        super().append(item)


class DefaultPanel(StepPanel):
    """Any step: banner, its files, its questions, its log."""

    main_tab = "files"

    def __init__(self, step: "StepDef", **kw):
        pid = safe_id(step.name)
        super().__init__(id=f"view-{pid}", **kw)
        self.step_name = step.name
        self._pid = pid
        self._title = step.title

    def compose(self) -> ComposeResult:
        yield ResultBanner()
        with TabbedContent():
            with TabPane("Files", id=f"{self._pid}-files"):
                yield FileViewer(self.step_name)
            with TabPane("Questions", id=f"{self._pid}-questions"):
                yield QuestionsTable(self.step_name)
            with TabPane("Log", id=f"{self._pid}-log"):
                yield StepLog(self.step_name, classes="refreshable")

    def show_tab(self, tab: str) -> bool:
        tabs = self.query(TabbedContent)
        if tabs and tabs.first().query(f"#{self._pid}-{tab}"):
            tabs.first().active = f"{self._pid}-{tab}"
            return True
        return False


# -- the spec panel (it addresses its step by its default id, so only for step id == kind) ------------------------


def _spec_panel(step: "StepDef") -> StepPanel:
    from q3tui.tui.views import SpecPanel

    if step.name != "spec":
        return DefaultPanel(step)
    return SpecPanel(id="view-spec")


register_panel("spec", _spec_panel)


def make_panel(step: "StepDef") -> StepPanel:
    """The panel of a step: its kind's registered one, else the default."""
    if step.kind not in PANELS:
        try:  # the VLSIT panels register themselves on import (lightweight: no pyslang / SDK)
            from q3tui.tui import vlsit_panels  # noqa: F401
        except ImportError:
            pass
    builder = PANELS.get(step.kind or step.name)
    return builder(step) if builder else DefaultPanel(step)
