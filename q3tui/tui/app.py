"""Q3TUI TUI (docs/spec/tui.md): pipeline dashboard over the same engine as the CLI."""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from pathlib import Path

from datetime import datetime

from rich.console import Group, RenderableType
from rich.markdown import Markdown as RichMarkdown
from rich.padding import Padding
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import ContentSwitcher, Footer, Input, Label, ListItem, ListView, RichLog, Static
from textual.css.query import NoMatches
from textual.worker import Worker

import asyncio

from q3tui.llm.assistant import Assistant
from q3tui.core.ops import Ops
from q3tui.core.events import Event, EventBus
from q3tui.pipeline.engine import Engine, EngineError, StepView
from q3tui.core.project import Project, read_json, write_json
from q3tui.tui.splitter import Splitter
from q3tui.tui.commands import CommandSuggester, hint as command_hint
from q3tui.tui.picker import Choice, ChoiceScreen, choices_from
from q3tui.tui.screens import EFFORTS, MODELS, ConfirmScreen, ResetScreen, SettingsScreen, StatsScreen, GateScreen, HistoryInput, ModelScreen, TextPromptScreen
from q3tui.tui.panels import StepLines, make_panel, safe_id
from q3tui.tui.views import fence_diagrams, SectionsPanel, SpecPanel, StepPanel

from q3tui.core.icons import icon as I
from q3tui.core.icons import set_style as set_icon_style
from q3tui.tui.i18n import set_lang, t

# step status -> (icon name, colour); the glyph comes from the icon set (settings: tui.icons)
ICONS = {
    "done": ("done", "green"), "user": ("user", "green"), "stale": ("stale", "yellow"), "pending": ("pending", "grey50"),
    "running": ("running", "cyan"), "failed": ("failed", "red"), "missing_input": ("missing", "yellow"), "unavailable": ("planned", "grey42"),
}


def legend_text() -> str:
    return (f"[green]{I('approved')}[/] {t('legend.approved')}   [yellow]{I('review')}[/] {t('legend.not_reviewed')}\n"
            f"[yellow]{I('stale')}[/] {t('legend.out_of_date')} [green]{I('user')}[/] {t('legend.yours')}\n"
            f"[cyan]{I('running')}[/] {t('legend.running')}    [red]{I('failed')}[/] {t('legend.failed')}\n"
            f"[grey50]{I('pending')}[/] {t('legend.no_result')}  [grey42]{I('planned')}[/] {t('legend.planned')}\n"
            f"[grey62]{I('edited')}[/] {t('legend.edited')}")
HELP = (
    "Keys: r run · x stop · a review · A approve all · g gate mode · , settings · s stats · e edit · C comment on selected text · o questions · f files · m model · R reset (step or all) · z zoom chat · drag the bars or ctrl+arrows to resize panes · 1-9 select step · "
    "i chat · / command (Tab or → completes) · Esc stops the assistant's reply, else back to the pipeline · ctrl+q quit.\n"
    "Commands: /run [from] [to] · /stop · /approve <step>|all [--force] · /change <step> <text> · /answer <id> <text> · /gate <step> <mode> · /gates · "
    "/model [id] [--save] · /effort <level> [--save] · /specreview on|off · /autoapprove on|off · /autoanswer on|off · "
    "/autoconfirm on|off · "
    "/parallel on|off [--multi-agent|--shared] [--save] · /settings [tab] · /stats [reset] · "
    "/reset <step> [--only] · /status · /open <file> · /cost [reset] · /newchat · /help · /quit. Anything else goes to the assistant."
)


def _short_error(text) -> str:
    """An error for the activity log: its first line, without the SDK's advice, at most 300 characters."""
    import re

    s = " ".join(str(text or "").split())
    s = re.sub(r"^(StageError|RuntimeError|EngineError): ", "", s)
    s = re.sub(r" You sent \(first \d+ of \d+ bytes\):.*", " (the JSON was invalid)", s)
    return s if len(s) <= 300 else s[:299] + "…"


class Q3TUIApp(App):
    TITLE = "Q3TUI"
    CSS_PATH = "app.tcss"
    BINDINGS = [
        Binding("r", "run", "Run"),
        Binding("x", "stop", "Stop"),
        Binding("a", "review", "Review"),
        Binding("A", "approve_all", "Approve all"),
        Binding("g", "gate_mode", "Gate mode"),
        Binding("e", "edit", "Edit"),
        Binding("C", "comment", "Comment"),
        Binding("o", "show_tab('questions')", "Questions"),
        Binding("f", "show_tab('files')", "Files", show=False),
        Binding("m", "pick_model", "Model"),
        Binding("comma", "settings", "Settings"),
        Binding("F", "flow_editor", "Flow"),
        Binding("K", "skill_editor", "Skills"),
        Binding("P", "prompt_editor", "Prompts"),
        Binding("s", "stats", "Stats"),
        Binding("ctrl+left", "resize('split-left', -4)", "Narrower pipeline", show=False),
        Binding("ctrl+right", "resize('split-left', 4)", "Wider pipeline", show=False),
        Binding("ctrl+up", "resize('split-activity', -2)", "Shorter step view", show=False),
        Binding("ctrl+down", "resize('split-activity', 2)", "Taller step view", show=False),
        Binding("R", "reset_step", "Reset"),
        Binding("z", "zoom_activity", "Zoom chat"),
        Binding("i", "focus_input", "Chat"),
        Binding("slash", "focus_input('/')", "Command", show=False),
        Binding("ctrl+l", "focus_input", "Chat", show=False),
        Binding("escape", "focus_steps", "Stop chat / leave input", show=False),
        Binding("question_mark", "help", "Help"),
        *[Binding(str(i + 1), f"select_step({i})", show=False) for i in range(9)],
    ]

    def __init__(self, project: Project, read_only: bool = False):
        super().__init__()
        self.project = project
        self.bus = EventBus()
        self.engine = Engine(project, self.bus)
        self.engine.read_only = read_only
        self.ops = Ops(self.engine)
        self._icons = project.cfg.tui.icons
        self._language = project.cfg.tui.language
        self.layout_file = project.state_dir / "tui" / "layout.json"
        self.layout_sizes: dict[str, int] = read_json(self.layout_file, default={}) or {}
        set_icon_style(self._icons)
        set_lang(self._language)
        from datetime import datetime

        self.session_start = datetime.now().isoformat(timespec="milliseconds")
        self.assistant = Assistant(self, project.state_dir / "tui" / "session.json")
        self.selected_step: str = self.engine.pipeline_steps[0].name
        self._item_steps = {safe_id(s.name): s.name for s in self.engine.steps}  # widget id fragment -> step id
        self.step_log: dict[str, StepLines] = {}  # per step: what it did (the default panel's Log tab)
        self._pipeline_worker: Worker | None = None
        self._assistant_worker: Worker | None = None
        self._last_outcome: str | None = None
        self._chat_focus: str | None = None
        self._refresh_pending = False
        self._dirty_panels: set[str] = set()
        self._gate_shown: str | None = None
        # live progress shown in the header while an LLM stage runs
        self._live: dict = {}
        self._spin = 0

    # -- layout ------------------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static(id="header")
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield Label("PIPELINE", classes="panel-title")
                yield ListView(*[ListItem(Static(id=f"step-{safe_id(s.name)}"), id=f"item-{safe_id(s.name)}") for s in self.engine.pipeline_steps],
                               id="steps")
                yield Static(legend_text(), id="legend")
            yield Splitter(vertical=True, id="split-left", min_size=20)
            with Vertical(id="right"):
                yield Label("", classes="panel-title", id="view-title")
                with ContentSwitcher(id="views", initial=f"view-{safe_id(self.engine.pipeline_steps[0].name)}"):
                    for step in self.engine.steps:
                        yield make_panel(step)
                yield Splitter(vertical=False, id="split-activity", min_size=3)
                yield Label("ACTIVITY", classes="panel-title", id="activity-title")
                yield RichLog(id="activity", wrap=True, markup=False, highlight=False, max_lines=3000)
                yield Static(id="cmd-hint", markup=False)
        yield HistoryInput(history_file=self.project.state_dir / "tui" / "history.json",
                           placeholder="Ask the assistant, or /help for commands (↑/↓ history, Tab completes)", id="cmd",
                           suggester=CommandSuggester(self._completions),
                           select_on_focus=False)  # else "/" from the shortcut is selected and the next key replaces it
        yield Footer()

    def on_mount(self) -> None:
        self.bus.subscribe(self._on_event)
        self.set_interval(1.0, self._tick)
        self._watching = self.engine.other_run()  # (before the summary: "next: run …" is wrong while another runs)
        views = self.engine.status()
        self._select_step(next((v.name for v in views if not v.ready and v.status != "unavailable"), views[0].name))
        self.refresh_all()
        self._log_line(Text.assemble(("Q3TUI ", "bold"), (f"· {self.project.root}\n", "grey50"), (HELP, "grey50")))
        self._log_summary()
        if self._watching:
            self._follow_run()
        self._w("#steps").focus()
        # no review popup at startup: it opens when a run reaches a gate; a waiting review is
        # in the summary line above ("next: review X (a)") and in the step's banner

    # -- rendering ---------------------------------------------------------------------------

    def refresh_all(self) -> None:
        """Update header, pipeline list and the visible step view (others update when shown)."""
        self._refresh_pending = False
        if self.project.cfg.tui.icons != self._icons or self.project.cfg.tui.language != self._language:  # changed in settings
            self._icons = self.project.cfg.tui.icons
            self._language = self.project.cfg.tui.language
            set_icon_style(self._icons)
            set_lang(self._language)
            self._w("#legend").update(legend_text())
        views = self.engine.status()
        self._render_header()
        self._render_steps(views)
        self._dirty_panels = {f"view-{safe_id(s.name)}" for s in self.engine.steps}
        self._refresh_panel(self.selected_step)

    def request_refresh(self) -> None:
        """Coalesce bursts of events into one refresh."""
        if not self._refresh_pending:
            self._refresh_pending = True
            self.set_timer(0.15, self.refresh_all)

    def _refresh_panel(self, step: str) -> None:
        pid = f"view-{safe_id(step)}"
        if pid in self._dirty_panels:
            self._dirty_panels.discard(pid)
            try:
                self._panel(step).refresh_view()
            except NoMatches:  # still mounting; stays dirty for the next refresh
                self._dirty_panels.add(pid)

    def _panel(self, step: str) -> StepPanel:
        if not hasattr(self, "_panels"):
            self._panels = {s.name: self.query_one(f"#view-{safe_id(s.name)}", StepPanel) for s in self.engine.steps}
        return self._panels[step]

    def _w(self, selector: str):
        """Cached query_one for fixed widgets (header, activity, steps, ...)."""
        cache = self.__dict__.setdefault("_widget_cache", {})
        if selector not in cache:
            cache[selector] = self.query_one(selector)
        return cache[selector]

    def _render_header(self) -> None:
        top = self.engine.state.top or "(new design)"
        live = ""
        if self._live:
            frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
            elapsed = int(time.monotonic() - self._live["since"])
            tokens = self._live.get("tokens")
            tok = f" · thinking ~{tokens / 1000:.1f}k tok" if tokens else ""
            live = (f"  [cyan]{frames[self._spin % len(frames)]} {self._live['who']} · {self._live.get('stage', '')}"
                    f" · {elapsed // 60}:{elapsed % 60:02d}{tok}[/]")
        self._w("#header").update(
            f"[b]Q3TUI[/b] · [b]{top}[/b] · [grey50]flow {self.engine.flow.name} · {self.project.root}[/]{live}"
            + (f"   [black on cyan] WATCHING a run in another process (pid {self._watching['pid']}) [/]"
               if getattr(self, "_watching", None) else "")
            + ("   [black on cyan] READ-ONLY [/]" if self.engine.read_only else "")
            + ("   [black on red] AUTO-ANSWER [/]" if self.project.cfg.pipeline.auto_answer else
               "   [black on yellow] AUTO-APPROVE [/]" if self.project.cfg.pipeline.auto_approve else "")
            + f"   [grey50]{self.project.cfg.llm.model} · pipeline ${self.engine.state.total_cost_usd:.2f}"
            f" · this session ${self.bus.cost_usd:.2f}[/]"
        )

    def _tick(self) -> None:
        if self._live:
            self._spin += 1
            self._render_header()
        # watching a run another process makes: follow its state every 2 s (this TUI saves nothing meanwhile)
        self._watch_tick = getattr(self, "_watch_tick", 0) + 1
        if self._watch_tick % 2 == 0 and not self._live:
            other = self.engine.other_run()
            if other or getattr(self, "_watching", None) or self.engine.read_only:
                was = getattr(self, "_watching", None)
                self._watching = other
                self.engine.reload()
                self._follow_run()
                self.refresh_all()
                if was and not other:  # the other run ended: where things stand now
                    self._log_summary()

    def _follow_run(self) -> None:
        """Watch mode: the other process's events (its run's events.jsonl) in the activity log, as if it ran here."""
        import json

        runs = sorted((self.project.state_dir / "runs").glob("run_*/events.jsonl"))
        if not runs:
            return
        path = runs[-1]
        pos = getattr(self, "_follow", None)
        start = pos[1] if pos and pos[0] == path else 0
        try:
            with path.open("rb") as f:
                f.seek(start)
                chunk = f.read()
        except OSError:
            return
        end = chunk.rfind(b"\n") + 1  # a line still being written is read next time
        self._follow = (path, start + end)
        events = []
        for raw in chunk[:end].splitlines():
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            if d.get("kind") in ("sdk", "progress"):
                continue
            ts = d.pop("ts", None)
            kind, step = d.pop("kind", ""), d.pop("step", None)
            try:
                when = datetime.fromisoformat(ts) if ts else datetime.now()
            except ValueError:
                when = datetime.now()
            events.append(Event(kind, step, d, when))
        if start == 0 and len(events) > 200:  # joining a long run: its latest part
            events = events[-200:]
        self._replaying = True
        try:
            for e in events:
                self._on_event(e)
        finally:
            self._replaying = False

    def _vlsit_badge(self, kind: str) -> str:
        from q3tui.tui.vlsit_panels import artifact

        rtm = (artifact(self, "rtm.json", {}) or {}).get("requirements", [])
        if not rtm:
            return ""
        if kind == "vlsit_verify":
            signed = sum(bool(r.get("signed_off")) for r in rtm)
            return f"[{'green' if signed == len(rtm) else 'yellow'}]{signed}/{len(rtm)} REQ[/]"
        traced = sum(bool(r.get("sva_traced")) for r in rtm)
        return f"[{'green' if traced == len(rtm) else 'yellow'}]{traced}/{len(rtm)} SVA[/]"

    def _render_steps(self, views: list[StepView]) -> None:
        modes = self.engine.gate_modes()
        for v in views:
            name, color = ICONS[v.status]
            icon = I(name)
            extra = []
            if v.gate == "open" and v.status in ("done", "user"):
                icon, color = I("review"), "yellow"  # has a result, not reviewed yet
                extra.append("[yellow]review[/]")
            if v.open_questions:
                extra.append(f"[yellow]{v.open_questions}?[/]")
            if v.status == "stale":
                extra.append("[yellow]stale[/]")
            if v.status == "user":
                extra.append("[green]yours[/]")
            if v.status == "unavailable":
                extra.append(f"[grey42]{v.detail.replace('planned for ', '')}[/]")
            if v.edited:
                extra.append(f"[grey62]{I('edited')}[/]")
            kind = self.engine.by_name[v.name].kind or v.name
            mode = modes.get(v.name)
            if mode in ("auto", "auto_answer"):
                extra.append(f"[grey62]{'auto' if mode == 'auto' else 'auto-ans'}[/]")
            if kind in ("vlsit_verify", "vlsit_sva") and v.status in ("done", "stale"):
                extra.append(self._vlsit_badge(kind))
            label = f"[{color}]{icon}[/] {self.engine.label(v.name):<2} {v.name:<13}" + " ".join(x for x in extra if x)
            w = self._w(f"#step-{safe_id(v.name)}")
            if getattr(w, "_last", None) != label:
                w._last = label
                w.update(label)

    def _log_summary(self) -> None:
        views = self.engine.status()
        parts = [f"{v.name} {('user-provided' if v.status == 'user' else v.status)}" for v in views if v.status not in ("unavailable",)]
        nxt = next((v for v in views if not v.ready), None)
        hint = ""
        watching = getattr(self, "_watching", None)
        if watching:
            running = next((v.name for v in views if v.status == "running"), None)
            hint = f"a run in another process (pid {watching['pid']}) is working" + (f" on {running}" if running else "")
        elif nxt is not None:
            if nxt.gate == "open" and nxt.status in ("done", "user"):
                hint = f"next: review {nxt.name} (a)"
            elif nxt.status == "missing_input":
                hint = f"next: {nxt.detail}"
            elif nxt.status == "unavailable":
                hint = f"next: {nxt.name} is {nxt.detail}"
            else:
                hint = f"next: run {nxt.name} (r)"
        n_q = sum(v.open_questions for v in views)
        qs = f" · {n_q} open question(s) (o)" if n_q else ""
        self._log_line(Text(f"status: {' · '.join(parts)}{qs}\n{hint}", "cyan"))

    def _log_line(self, text: RenderableType) -> None:
        self._w("#activity").write(text)

    def _log_chat(self, who: str, text: str, style: str) -> None:
        """A chat message: label line + Markdown body, scrolled so its start is visible."""
        log = self._w("#activity")
        top = log.virtual_size.height
        body = RichMarkdown(fence_diagrams(text.strip()), code_theme="ansi_dark") if who == "assistant" else Text(text.strip())
        log.write(Group(Text.assemble((f"{datetime.now():%H:%M:%S} ", "grey42"), (who, style)), Padding(body, (0, 0, 0, 2))))
        if who == "assistant":
            # long replies: keep the beginning of the message on screen instead of only its tail
            self.call_after_refresh(lambda: log.scroll_to(y=top, animate=False))

    # -- events --------------------------------------------------------------------------------

    def _track_live(self, e: Event) -> None:
        d, k = e.data, e.kind
        who = e.step or ""
        if k == "stage" and d.get("status") == "llm_start":
            self._live = {"who": who, "stage": d.get("name", ""), "since": time.monotonic()}
        elif k == "progress" and self._live:
            self._live["tokens"] = d.get("thinking_tokens")
        elif k in ("llm_text", "tool_call") and self._live:
            self._live["tokens"] = None  # thinking finished; now acting
        elif k in ("llm_done", "step_finished", "run_finished"):
            self._live = {}
        self._render_header()

    def _on_event(self, e: Event) -> None:
        d, k = e.data, e.kind
        if k in ("stage", "progress", "llm_text", "tool_call", "llm_done", "step_finished", "run_finished") \
                and not getattr(self, "_replaying", False):  # (another process's run: not this TUI's own)
            self._track_live(e)
        if k == "progress":
            return
        ts = e.ts.strftime("%H:%M:%S")
        tag = (e.step or "").ljust(9)

        def line(msg: str, style: str = "") -> None:
            self._log_line(Text.assemble((f"{ts} ", "grey42"), (tag, "bold"), (msg, style)))
            if e.step in self.engine.by_name:  # (the default panel's Log tab)
                self.step_log.setdefault(e.step, StepLines()).append((ts, style, msg))

        if e.step == "assistant":
            if k == "llm_text":
                self._log_chat("assistant", d.get("text", ""), "bold magenta")
            elif k == "tool_call":
                line(f"→ {d.get('tool')} {d.get('input', '')}", "magenta")
            return
        if k == "step_started":
            self._select_step(e.step)  # lower panel follows the running step
            line(f"▶ {d.get('title', '')}" + (f" (applying {len(d['feedback'])} change request(s))" if d.get("feedback") else ""), "bold")
        elif k == "step_skipped":
            line(str(d.get("reason")), "grey50")
        elif k == "stage" and d.get("status") == "llm_start":
            line(f"{d.get('name')} …", "grey50")
        elif k == "llm_text":
            txt = " ".join(d.get("text", "").split())
            line(txt[:300] + ("…" if len(txt) > 300 else ""), "grey62")
        elif k == "tool_call":
            line(f"→ {d.get('tool')} {d.get('input', '')}", "cyan")
        elif k == "llm_done":
            line(f"{d.get('stage')}: {d.get('turns')} turns · ${d.get('cost_usd', 0):.2f}", "grey50")
        elif k == "log":
            line(f"• {d.get('message')}")
        elif k == "warning":
            line(f"! {d.get('message')}", "yellow")
        elif k == "error":
            msg = _short_error(d.get("message"))
            if msg != getattr(self, "_last_failure", None):  # (the step's failure line already said it)
                line(f"{I('failed')} {msg}", "red")
        elif k == "step_finished":
            if d.get("status") == "done":
                line(f"{I('done')} done · ${d.get('cost_usd', 0):.2f}", "green")
            elif d.get("status") == "cancelled":
                line("cancelled — press r to run it again", "yellow")
            else:
                self._last_failure = _short_error(d.get("error"))
                line(f"{I('failed')} failed: {self._last_failure}", "red")
        elif k == "gate_opened":
            line(f"{I('review')} waiting for review ({d.get('open_questions', 0)} open question(s))", "yellow")
        elif k == "gate_resolved":
            line(f"review: {d.get('decision')}", "green" if d.get("decision") == "approved" else "yellow")
        elif k == "run_finished":
            line(f"run finished: {d.get('outcome')}", "grey50")

        if k in ("step_started", "step_finished", "gate_opened", "gate_resolved", "questions", "run_finished", "step_skipped", "log"):
            self.request_refresh()
        if k == "step_finished" and d.get("status") == "done" and e.step:
            self._select_step(e.step)
            panel = self.current_panel()
            if panel.main_tab:
                panel.show_tab(panel.main_tab)
        if k == "gate_opened":
            self.call_after_refresh(self._maybe_show_gate)

    # -- pipeline control ---------------------------------------------------------------------

    def start_run(self, start: str | None = None, stop: str | None = None, only: str | None = None,
                  regenerate: set[str] | None = None) -> str:
        if self._pipeline_worker and self._pipeline_worker.is_running:
            return "a run is already in progress"
        self._gate_shown = None

        async def go() -> None:
            outcome = await self.engine.run(start=start, stop=stop, only=only, regenerate=regenerate)
            self._last_outcome = outcome
            self.refresh_all()
            if outcome == "complete":
                self.notify("pipeline complete")
            elif outcome == "unavailable":
                self.notify("reached a step that is not implemented yet", severity="warning")

        self._pipeline_worker = self.run_worker(go(), group="pipeline", exclusive=True, exit_on_error=False)
        return "run started" + (f" from {start}" if start else "") + (f" to {stop}" if stop else "") + (f" (only {only})" if only else "")

    def focus_step(self) -> str | None:
        """Step the user is working on, for the assistant's scope checks."""
        return self._chat_focus or self.selected_step

    async def run_and_wait(self, start: str | None = None, stop: str | None = None, only: str | None = None,
                           regenerate: set[str] | None = None) -> str:
        """Start a run and wait for it to stop (used by the assistant to chain actions in one turn)."""
        msg = self.start_run(start, stop, only, regenerate)
        worker = self._pipeline_worker
        if worker is None or msg.startswith("a run is already"):
            return msg
        from textual.worker import WorkerCancelled, WorkerFailed

        try:
            await worker.wait()
        except (WorkerCancelled, WorkerFailed) as exc:
            return f"run did not finish: {type(exc).__name__}"
        views = {v.name: v for v in self.engine.status()}
        summary = ", ".join(f"{n} {v.status}" + (" (review open)" if v.gate == "open" and v.status in ("done", "user") else "")
                            for n, v in views.items() if v.status != "unavailable")
        return f"run finished: {self._last_outcome or 'stopped'}; {summary}"

    def stop_run(self) -> str:
        if self._pipeline_worker and self._pipeline_worker.is_running:
            self._pipeline_worker.cancel()
            return "stopping"
        return "nothing is running"

    def stop_chat(self) -> str:
        """Stop the assistant's reply (its conversation continues from the last finished reply)."""
        if self._assistant_worker and self._assistant_worker.is_running:
            self._assistant_worker.cancel()
            self._log_line(Text("assistant stopped — ask again or carry on (Esc/x stops a reply)", "yellow"))
            self.refresh_all()
            return "assistant stopped"
        return "the assistant is not answering"

    def stop_any(self) -> str:
        """x and /stop: the pipeline run if one is running, else the assistant's reply."""
        if self._pipeline_worker and self._pipeline_worker.is_running:
            return self.stop_run()
        return self.stop_chat() if self._assistant_worker and self._assistant_worker.is_running else "nothing is running"

    def _open_gate(self) -> StepView | None:
        return next((v for v in self.engine.status() if v.gate == "open" and v.status in ("done", "user")), None)

    def _maybe_show_gate(self) -> None:
        view = self._open_gate()
        if view is None or self._gate_shown == view.name or isinstance(self.screen, (GateScreen, TextPromptScreen)):
            return
        self._gate_shown = view.name
        # no dialog pops up over what you are reading: a note says it is waiting; `a` (or /approve) opens the review.
        # warning severity + a symbol + a longer timeout: this is easy to miss if you are watching the pipeline
        # column's status colours instead of the toast corner.
        self.notify(f"⚠ {self.engine.label(view.name)} {view.name} is waiting for your review — press a", title="⚠ Review needed",
                   severity="warning", timeout=15)

    def _show_gate(self, view: StepView) -> None:
        step = self.engine.by_name[view.name]
        files = ", ".join(self.project.rel(f) for f in step.outputs(self.engine))
        open_q = [q for q in self.engine.questions() if q["step"] == view.name and q["status"] == "open"]
        blocking = [q["id"] for q in open_q if q.get("blocking")]
        if blocking:
            qnote = f"[red]{len(blocking)} blocking question(s) ({', '.join(blocking)}) must be answered before approving.[/]"
        elif open_q:
            qnote = f"{len(open_q)} open question(s): approving accepts their default assumptions (already in the text)."
        else:
            qnote = "No open questions."
        summary = f"Outputs: {files}\n{qnote}"
        kind = step.kind or view.name
        if kind.startswith("vlsit_"):
            from q3tui.tui import vlsit_panels

            head = vlsit_panels.HEADERS.get(kind)
            if head is not None:
                summary = head(self) + "\n" + summary

        def done(choice: str | None) -> None:
            self._gate_choice(view.name, choice or "later")

        self.push_screen(GateScreen(view.name, summary, mode=self.engine.gate_mode(view.name) or "human", blocking=len(blocking),
                                    open_questions=len(open_q), label=f"{self.engine.label(view.name)} {view.name}".strip()), done)

    def _gate_choice(self, step: str, choice: str) -> None:
        if choice == "approve":
            try:
                self.log_info(self.engine.approve(step))
            except EngineError as exc:
                self._log_line(Text(str(exc), "red"))
                self._select_step(step)
                self.action_show_tab("questions")
                self._gate_shown = None
                return
            self.refresh_all()
            self.start_run()
        elif choice == "questions":
            self._select_step(step)
            self.action_show_tab("questions")
        elif choice == "edit":
            main = self.engine.by_name[step].outputs(self.engine)
            target = next((f for f in main if f.suffix in (".md", ".json") and "question" not in f.name and f.name != "contract.json"), None)
            if target:
                self.edit_file(target)
            self._gate_shown = None
            self.call_after_refresh(self._maybe_show_gate)
        elif choice == "reject":
            def feedback(text: str | None) -> None:
                if text:
                    self._log_line(self.engine.request_change(step, text))
                    self.start_run()
                else:
                    self._gate_shown = None

            self.push_screen(self.prompt_screen(f"Request changes: {step}", "What should change? The step re-runs with this feedback."), feedback)

    # -- actions ---------------------------------------------------------------------------------

    def set_model(self, model: str | None = None, effort: str | None | bool = False, save: bool = False) -> str:
        msg = self.ops.set_model(model, effort, save)
        self._render_header()
        return msg

    async def confirm(self, question: str, detail: str = "") -> bool:
        """Ask the user (used by the assistant for destructive actions)."""
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self.push_screen(ConfirmScreen(question, detail), lambda ok: future.done() or future.set_result(bool(ok)))
        return await future

    def action_settings(self, tab: str | None = None) -> None:
        """Settings dialog; opens on the selected step's tab when it has one."""
        from q3tui.core.settings import TABS

        tabs = [t.id for t in TABS] + ["gates"]
        here = self.selected_step
        tab = tab if tab in tabs else (here if here in tabs else "general")

        def done(result: dict | None) -> None:
            if not result or not (result["changes"] or result.get("gates")):
                return
            try:
                if result["changes"]:
                    self.ops.apply_settings(result["changes"], save=result["save"])
                for step, mode in (result.get("gates") or {}).items():
                    self.ops.set_gate_mode(step, mode, save=result["save"])
            except EngineError as exc:
                self._log_line(Text(str(exc), "red"))
            self._render_header()
            self.refresh_all()

        self.push_screen(SettingsScreen(self.ops.settings(), tab, gates=self._gate_rows()), done)

    def action_flow_editor(self) -> None:
        """F: the flow editor — add / remove / move steps, edit deps, gate, view, pass conditions and notes."""
        from q3tui.steps import KINDS, make_step
        from q3tui.tui.screens import FlowScreen

        eng = self.engine
        rows = []
        for s in eng.pipeline_steps:
            spec = eng.flow.spec(s.name)
            deps = spec.deps if spec and spec.deps is not None else list(s.deps)
            rows.append({"step": s.name, "title": f"{eng.label(s.name)} {s.title}".strip(), "kind": s.kind or s.name,
                         "deps": list(deps), "gate": eng.gate_mode(s.name) or "none",
                         "view": list(spec.view) if spec else [], "pass": list(spec.passes) if spec else [],
                         "notes": spec.notes if spec else "", "label": spec.label if spec else None,
                         "raw_title": spec.title if spec else None, "options": dict(spec.options) if spec else {}})

        def done(result: dict | None) -> None:
            if not result:
                return
            try:
                for step, ch in result["changes"].items():
                    for key in ("view", "pass", "notes"):
                        if key in ch:
                            self.ops.set_step_setting(step, key, ch[key], save=result["save"])
                    if "gate" in ch:
                        self.ops.set_gate_mode(step, ch["gate"], save=result["save"])
                if result["structure"]:
                    self.ops.save_flow_steps([
                        {"id": r["step"], "kind": r["kind"], "title": r["raw_title"], "label": r["label"], "deps": r["deps"],
                         "gate": None if r["gate"] in (None, "none") else r["gate"], "options": r["options"], "view": r["view"],
                         "pass": r["pass"], "notes": r["notes"]} for r in result["structure"]])
            except EngineError as exc:
                self._log_line(Text(str(exc), "red"))
            self.refresh_all()

        self.push_screen(FlowScreen(rows, sorted(KINDS), self.selected_step, eng.flow.name), done)

    def action_skill_editor(self) -> None:
        """K: read and edit the skill (skill.md) of a step of a flow folder."""
        from q3tui.tui.screens import SkillScreen

        try:
            steps = self.ops.skill_steps()
        except EngineError as exc:
            self.notify(str(exc), severity="warning")
            return
        self.push_screen(SkillScreen(steps, self.ops.skill_get, self.ops.skill_set, self.selected_step), lambda _: self.refresh_all())

    def action_prompt_editor(self) -> None:
        """P: read and customise the system prompts of the steps and stages."""
        from q3tui.tui.screens import PromptsScreen

        self.push_screen(PromptsScreen(self.ops.prompt_targets(), self.ops.prompt_get, self.ops.set_prompt, self.selected_step))

    def _gate_rows(self) -> list[tuple[str, str, str]]:
        """(step, title, mode) of every step that can have a review gate in this flow (Settings → Gates)."""
        modes = self.engine.gate_modes()
        return [(s.name, f"{self.engine.label(s.name)} {s.title}".strip(), modes.get(s.name, "none")) for s in self.engine.pipeline_steps]

    def action_stats(self) -> None:
        from q3tui.core import stats

        order = [s.name for s in self.engine.steps] + ["assistant"]
        self.push_screen(StatsScreen(lambda since: stats.report(self.project.state_dir, since), self.session_start, order,
                                     reset=self.ops.reset_stats))

    def action_pick_model(self) -> None:
        def done(result: dict | None) -> None:
            if result:
                self.set_model(result["model"], result["effort"], result["save"])

        self.push_screen(ModelScreen(self.project.cfg.llm.model, self.project.cfg.llm.effort), done)

    async def action_quit(self) -> None:
        if not (self._pipeline_worker and self._pipeline_worker.is_running):
            self.exit()
            return

        def done(ok: bool | None) -> None:
            if ok:
                self.exit()

        self.push_screen(ConfirmScreen(f"'{self.engine.running}' is running. Quit and cancel it?", "Its output so far is discarded; run it again later with r."), done)

    def action_reset_step(self, step: str | None = None, only: bool = False) -> None:
        """R: choose between resetting the selected step (and the steps after it) and everything.
        `/reset <step> [--only]` and `/reset all` ask a plain confirmation."""
        chosen = step
        step = step or self.selected_step
        if self._pipeline_worker and self._pipeline_worker.is_running:
            self.notify("stop the running step first (x)", severity="warning")
            return
        first = self.engine.steps[0].name
        try:
            plan = self.engine.reset_plan(first if step == "all" else step, only)
            all_plan = self.engine.reset_plan(first)
        except EngineError as exc:
            self.notify(str(exc), severity="error")
            return

        def do_reset(target: str, target_only: bool, target_plan: dict) -> None:
            self.engine.reset(target, target_only)
            self._gate_shown = None
            # the assistant's history talks about the files that were just deleted: start fresh
            self.assistant.reset()
            self.log_info("assistant conversation restarted (its history referred to the reset outputs)")
            self.refresh_all()
            self.notify(f"reset {', '.join(target_plan)} — press r to run from scratch")

        if chosen is None:  # the key: offer both
            def picked(choice: str | None) -> None:
                if choice == "step":
                    do_reset(step, False, self.engine.reset_plan(step))
                elif choice == "all":
                    do_reset(first, False, all_plan)

            self.push_screen(ResetScreen(step, self.engine.reset_plan(step), all_plan, self.project.rel), picked)
            return
        files = [self.project.rel(f) for fs in plan.values() for f in fs]
        detail = ("Deletes: " + ", ".join(files) if files else "No generated files; clears review/answer state.") + \
            "\nYour inputs (intent, template, own files) are kept; deleted files are backed up to .q3tui/bkp/."
        target = first if step == "all" else step
        self.push_screen(ConfirmScreen(f"Reset {', '.join(plan)}?", detail),
                         lambda ok: do_reset(target, only and step != "all", plan) if ok else None)

    def _guard_run(self) -> bool:
        """A run is already going: if you are not looking at a step that's running, offer to go back to
        one instead of silently refusing — jumping to another step and pressing r by mistake must not look
        like nothing happened. Returns True when it is fine to start a run now."""
        if not (self._pipeline_worker and self._pipeline_worker.is_running):
            return True
        running = self.engine.running_steps
        if running and self.selected_step not in running:
            target = running[0]

            def back(ok: bool | None) -> None:
                if ok:
                    self._select_step(target)

            names = " and ".join(running)
            self.push_screen(ConfirmScreen(f"'{names}' still running.",
                                           "Go back to it? A second run cannot start while one is going — "
                                           "stop it there (x) first if you want to run something else."), back)
        else:
            self.notify("a run is already in progress")
        return False

    def action_run(self) -> None:
        if self._guard_run():
            self.notify(self.start_run())

    def action_stop(self) -> None:
        self.notify(self.stop_any())

    def action_review(self) -> None:
        view = self._open_gate()
        if view is None:
            self.notify("nothing waiting for review")
            return
        self._gate_shown = view.name
        self._show_gate(view)

    def action_approve_all(self) -> None:
        pending = self.engine.pending_approvals()
        if not pending:
            self.notify("nothing waiting for review")
            return

        def done(ok: bool | None) -> None:
            if ok:
                self._approve_all()

        self.push_screen(ConfirmScreen(f"Approve {', '.join(pending)}?",
                                       "Open non-blocking questions take their default answers; steps with blocking questions are skipped."), done)

    def _approve_all(self, force: bool = False) -> None:
        lines = self.engine.approve_all(force=force)
        for line in lines:
            self._log_line(Text(line, "yellow" if line.startswith(("skipped", "out of date")) else "cyan"))
        self._gate_shown = None
        self.refresh_all()
        if any(line.startswith("approved") for line in lines):
            self._continue_run()

    def _continue_run(self) -> None:
        """After an approval the pipeline carries on by itself: your decisions flow into the spec,
        the requirements, the architecture, … (stops again at the next review or blocking question)."""
        if self._pipeline_worker and self._pipeline_worker.is_running:
            return
        self._log_line(Text("continuing the run…", "grey50"))
        self.start_run()

    def current_panel(self) -> StepPanel:
        return self._panel(self.selected_step)

    def action_show_tab(self, tab: str) -> None:
        if tab == "questions" and not self.current_panel().show_tab(tab):
            with_q = next((v.name for v in self.engine.status() if v.open_questions), self.engine.pipeline_steps[0].name)
            self._select_step(with_q)
        self.current_panel().show_tab(tab)

    def remember_layout(self, splitter: str, size: int | None) -> None:
        """A splitter was dragged (or reset with a double-click): keep the size for next time."""
        if size is None:
            self.layout_sizes.pop(splitter, None)
        else:
            self.layout_sizes[splitter] = size
        write_json(self.layout_file, self.layout_sizes)

    def action_resize(self, splitter: str, delta: int) -> None:
        """ctrl+←/→: pipeline column width; ctrl+↑/↓: step view height (activity takes the rest, below it)."""
        sp = self.query_one(f"#{splitter}", Splitter)
        pane = sp.pane
        if pane is None:
            return
        current = pane.outer_size.width if sp.vertical else pane.outer_size.height
        sp.set_size(current + delta)
        self.call_after_refresh(lambda: self.remember_layout(splitter, pane.outer_size.width if sp.vertical else pane.outer_size.height))

    def action_zoom_activity(self) -> None:
        """Toggle a full-height activity/chat panel (hides the step view)."""
        for wid in ("#views", "#view-title", "#split-activity"):
            w = self.query_one(wid)
            w.display = not w.display
        self._w("#activity").scroll_end(animate=False)

    def action_focus_steps(self) -> None:
        """Esc: stop the assistant while it answers; otherwise leave the input."""
        if self._assistant_worker and self._assistant_worker.is_running:
            self.stop_chat()
            return
        self._w("#steps").focus()

    def action_focus_input(self, prefix: str = "") -> None:
        cmd = self.query_one("#cmd", Input)
        if prefix and not cmd.value:
            cmd.value = prefix
            cmd.cursor_position = len(prefix)
        cmd.focus()

    def action_help(self) -> None:
        self._log_line(Text(HELP, "grey62"))

    def action_select_step(self, index: int) -> None:
        if index < len(self.engine.pipeline_steps):
            self._select_step(self.engine.pipeline_steps[index].name)

    def action_edit(self) -> None:
        panel = self.current_panel()
        target = panel.current_file()
        if isinstance(panel, SpecPanel) and panel.query_one("TabbedContent").active == "spec-sections":
            target = self.project.spec_dir / "spec.md"
        if target is None or not target.is_file():
            self.notify("nothing to edit here yet (Files tab lists this step's files)")
            return
        self.edit_file(target)

    def edit_file(self, path: Path) -> None:
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
        with self.suspend():
            subprocess.run([*shlex.split(editor), str(path)])
        self.refresh_all()
        self.log_info(f"edited {self.project.rel(path)} — downstream steps are now stale if it changed")

    def _select_step(self, name: str | None) -> None:
        if not name or name not in self.engine.by_name:
            return
        self.selected_step = name
        self._w("#views").current = f"view-{safe_id(name)}"
        names = [s.name for s in self.engine.pipeline_steps]
        index = names.index(name)
        steps = self._w("#steps")
        if steps.index != index:
            # moving the highlight from code must not come back as a "user highlighted" event:
            # those arrive late and used to bounce the view between steps (ping-pong)
            with steps.prevent(ListView.Highlighted):
                steps.index = index
        self._refresh_panel(name)
        label = self.engine.label(name)
        self._w("#view-title").update(f"STEP {label} · {self.engine.by_name[name].title.upper()}")

    def view_context(self) -> str:
        """One line for the assistant: which step/tab is on screen and what is selected."""
        from textual.widgets import DataTable, TabbedContent

        step = self.engine.by_name[self.selected_step]
        parts = [f"viewing step {self.engine.label(step.name)} {step.name} ({step.title})"]
        try:
            panel = self.current_panel()
            tabs = panel.query(TabbedContent)
            if tabs:
                pane = tabs.first().active_pane
                if pane is not None:
                    parts.append(f"tab '{pane._title if hasattr(pane, '_title') else pane.id}'")
                    tables = [t for t in pane.query(DataTable) if t.row_count]
                    if tables:
                        key = tables[0].coordinate_to_cell_key(tables[0].cursor_coordinate).row_key.value
                        parts.append(f"selected row: {key}")
            files = [self.project.rel(f) for f in step.outputs(self.engine) if f.is_file()][:4]
            if files:
                parts.append(f"files: {', '.join(files)}")
        except NoMatches:
            pass
        return "; ".join(parts)

    # -- helpers used by the step views -------------------------------------------------------

    def log_info(self, message: str) -> None:
        self._log_line(Text(message, "cyan"))

    def prompt_screen(self, title: str, body: str = "", value: str = "", multiline: bool = True) -> TextPromptScreen:
        return TextPromptScreen(title, body, value=value, multiline=multiline)

    def answer_question(self, qid: str) -> None:
        q = next((q for q in self.engine.questions() if q["id"] == qid), None)
        if q is None:
            return
        if q.get("history"):
            how = "default accepted" if q["status"] == "default" else "answered"
            self.log_info(f"{qid} ({how} {q.get('closed_at', '').replace('T', ' ')}): {q['question']} → {q['answer']}  "
                          "— no longer asked by the latest output; request a change (c) to revisit it")
            return

        def apply(text: str | None) -> None:
            if text:
                waiting = self.engine.state.gates.get(q["step"])
                self.log_info(self.ops.answer(qid, text))
                self.refresh_all()
                if waiting and waiting.for_questions and q["step"] not in self.engine.state.gates:
                    self._continue_run()  # its last blocking question is answered: carry on

        self.push_screen(self.question_picker(q), lambda result: apply(result.answer) if result is not None else None)

    def question_picker(self, q: dict) -> ChoiceScreen:
        """The picker for one question: its suggested options (else the assumed answer), the current answer marked, and
        Other… to type. A proposal (a step that only proposes) offers Accept / Reject."""
        qid = q["id"]
        target = self.engine.gap_target(q["step"], q)
        where = f"\n\nYour answer goes to: {target}" if target else ""
        assumed = (q.get("default_assumption") or "").strip()
        current = (q.get("answer") or "").strip()
        body = f"{q['question']}\n\nAssumed so far: {assumed or '—'}{where}"
        step = self.engine.by_name.get(q["step"])
        if step is not None and getattr(step, "proposes_only", False):
            choices = [Choice(assumed or "yes", "Accept the proposal", assumed or "apply it as suggested", recommended=True),
                       Choice("no", "Reject", "nothing is sent anywhere")]
            other = "Edit the proposal…"
        else:
            choices = choices_from(q.get("options"), recommended=assumed)
            if assumed and not any(c.value.strip() == assumed for c in choices):
                choices.insert(0, Choice(assumed, assumed if len(assumed) <= 70 else assumed[:69] + "…",
                                         "what is assumed in the text now" if len(assumed) <= 70 else assumed, recommended=True))
            elif not choices and not assumed:
                choices = []
            other = "Other…"
        for c in choices:
            if current and c.value.strip() == current:
                c.description = ("your current answer" + (f" — {c.description}" if c.description else ""))
        return ChoiceScreen(f"Answer {qid}" + (" (blocking)" if q.get("blocking") else ""), choices, body=body, other=other,
                            other_title=f"Answer {qid}", other_value=current or assumed)

    async def ask_user(self, questions: list[dict]) -> list[str]:
        """The assistant's `ask_user`: one picker per question ({question, header?, options: [{label, description}], multi?}),
        the answers come back as text (several picks joined with ", ")."""
        answers: list[str] = []
        for item in questions:
            future: asyncio.Future = asyncio.get_running_loop().create_future()
            options = choices_from(item.get("options"))
            for c in options:
                c.recommended = c.recommended
            screen = ChoiceScreen(str(item.get("header") or "Question from the assistant"), options, body=str(item.get("question", "")),
                                  multi=bool(item.get("multi")), other="Other…")
            self.push_screen(screen, lambda result, f=future: f.done() or f.set_result(result))
            result = await future
            answers.append("(the user dismissed the question)" if result is None else result.answer)
        return answers

    # -- gate modes ------------------------------------------------------------------------------

    def action_gate_mode(self, step: str | None = None) -> None:
        """g: choose the gate mode of the selected step (human / auto / auto-answer / none) in a picker."""
        step = step or self.selected_step
        now = self.engine.gate_mode(step) or "none"
        modes = [("human", "human", "stops for your review"), ("auto", "auto", "approves itself; blocking questions still stop it"),
                 ("auto_answer", "auto-answer", "unattended: approves itself, questions take their defaults"),
                 ("none", "no gate", "no review; blocking questions still stop the pipeline")]
        choices = [Choice(v, label + (" — current" if v == now else ""), desc, recommended=v == now) for v, label, desc in modes]

        def done(result) -> None:
            if result is not None and result.value:
                try:
                    self.log_info(self.ops.set_gate_mode(step, result.value, save=True))  # (kept for next time)
                except EngineError as exc:
                    self._log_line(Text(str(exc), "red"))
                self.refresh_all()

        self.push_screen(ChoiceScreen(f"Gate mode: {step}", choices, body="Saved to q3tui.yaml (pipeline.gate_modes). "
                                      "Project-wide auto-approve / auto-answer switches still apply on top.", other=None), done)

    # -- widget events ---------------------------------------------------------------------------

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        """Only the user's own navigation (arrow keys / mouse) gets here; see _select_step."""
        if event.item and event.item.id:
            name = self._item_steps.get(event.item.id.removeprefix("item-"), event.item.id.removeprefix("item-"))
            if name != self.selected_step:
                self._select_step(name)

    def _completions(self, kind: str) -> list[str]:
        """Values offered when completing a slash-command argument (see tui/commands.py)."""
        steps = [s.name for s in self.engine.steps]
        if kind == "step":
            return steps
        if kind == "step_all":
            return [*steps, "all"]
        if kind == "gate":
            return [*self.engine.gate_modes(), "all"]
        if kind == "gate_mode":
            return ["human", "auto", "auto_answer", "none"]
        if kind == "question":
            return [q["id"] for q in self.engine.questions() if q.get("status") == "open"]
        if kind == "model":
            return [m for m, _ in MODELS]
        if kind == "effort":
            return [*EFFORTS, "none"]
        if kind == "file":
            return sorted({self.project.rel(p) for st in self.engine.steps for p in st.outputs(self.engine) if p.is_file()})
        return []

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "cmd":
            return
        text = command_hint(event.value, self._completions) if event.value.startswith("/") else ""
        box = self._w("#cmd-hint")
        box.display = bool(text)
        if text:
            box.update(text)

    def selected_text(self) -> str:
        """What is selected on screen (mouse drag in a rendered file or a text area), else ''."""
        try:
            from textual.widgets import TextArea

            if isinstance(self.focused, TextArea) and self.focused.selected_text:  # (a text area keeps its own selection)
                return self.focused.selected_text.strip()
            return (self.screen.get_selected_text() or "").strip()
        except Exception:  # noqa: BLE001 - a selection is a convenience, never a crash
            return ""

    def highlighted_item(self, panel) -> tuple[str, str] | None:
        """(what, its text) of the row highlighted in the shown tab of a panel: a spec section (its written text), else any table
        row (requirement, port, test, assertion, question…) as `column: value` lines — so every view can be commented on."""
        from textual.widgets import DataTable, TabbedContent

        tabs = panel.query(TabbedContent)
        active = tabs.first().active if tabs else ""
        if isinstance(panel, SpecPanel) and active == "spec-sections":
            return panel.query_one(SectionsPanel).comment_target()
        pane = panel.query_one(f"#{active}") if active else panel
        tables = [t for t in pane.query(DataTable) if t.display and t.row_count]
        table = next((t for t in tables if t.has_focus), tables[0] if tables else None)
        if table is None:
            return None
        try:
            cells = table.get_row_at(table.cursor_row)
        except Exception:  # noqa: BLE001
            return None
        heads = [str(getattr(c.label, "plain", c.label)) for c in table.columns.values()]
        lines = [f"{h}: {getattr(v, 'plain', v)}".strip() if h.strip() else str(getattr(v, "plain", v)) for h, v in zip(heads, cells)]
        what = f"step {self.selected_step}" + (f", tab {active.split('-', 1)[-1]}" if active else "")
        return f"{what}, row {getattr(cells[0], 'plain', cells[0])}", "\n".join(x for x in lines if x.strip())

    def action_comment(self) -> None:
        """C: comment on what you selected (mouse drag) or, with nothing selected, the highlighted row / section of the shown tab, or the
        shown file; the assistant fixes it."""
        if self._assistant_worker and self._assistant_worker.is_running:
            self.notify("the assistant is still answering — Esc stops it, or comment when it is done", severity="warning")
            return
        panel = self.current_panel()
        target = panel.current_file()
        quote = self.selected_text()
        where = None
        if not quote:  # nothing selected with the mouse: the highlighted thing of the shown tab (section, requirement, port…)
            found = self.highlighted_item(panel)
            if found:
                where, quote = found
        if where is None:
            if target is None and not quote:
                self.notify("highlight a row or select some text (mouse drag), then press C", severity="warning")
                return
            where = f"`{self.project.rel(target)}`" if target else f"step {self.selected_step}"
        shown = quote if len(quote) <= 600 else quote[:600] + " …"

        def done(comment: str | None) -> None:
            if not comment or not comment.strip():
                return
            msg = f"Comment on {where} (step {self.selected_step}):\n"
            if quote:
                msg += "".join(f"> {ln}\n" for ln in shown.splitlines())
            msg += f"\n{comment.strip()}\n\nPlease fix this with your tools (a change request for the step, or edit the section)."
            self._send_to_assistant(msg)

        self.push_screen(TextPromptScreen(f"Comment on {where.replace(chr(96), '')}", body=shown or "(no text selected: the comment is about the file)",
                                          placeholder="what should change?"), done)

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "cmd":
            return
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        if isinstance(event.input, HistoryInput):
            event.input.remember(text)
        if text.startswith("/"):
            self._command(text)
            return
        if self._assistant_worker and self._assistant_worker.is_running:
            event.input.value = text  # keep what they typed
            self.notify("the assistant is still answering — Esc stops it, or send again when it is done", severity="warning")
            return
        self._send_to_assistant(text)

    def _send_to_assistant(self, text: str) -> None:
        self._log_chat("you", text, "bold blue")

        # the step the user is working on is fixed when they send the message (the view may
        # follow a running step afterwards)
        self._chat_focus = self.selected_step
        context = self.view_context()

        async def ask() -> None:
            try:
                await self.assistant.ask(text, self.bus.scoped("assistant"), context=context)
            except Exception as exc:  # noqa: BLE001 - shown to the user
                self._log_line(Text(f"assistant error: {exc}", "red"))
            self.refresh_all()

        self._assistant_worker = self.run_worker(ask(), group="assistant", exit_on_error=False)

    def _command(self, text: str) -> None:
        parts = shlex.split(text[1:]) if text[1:].strip() else ["help"]
        cmd, args = parts[0], parts[1:]
        try:
            if cmd == "run":
                if self._guard_run():
                    self._log_line(self.start_run(args[0] if args else None, args[1] if len(args) > 1 else None))
            elif cmd == "stop":
                self._log_line(self.stop_any())
            elif cmd == "approve" and args and args[0] == "all":
                self._approve_all(force="--force" in args)
            elif cmd == "approve":
                self.log_info(self.engine.approve(args[0], force="--force" in args))
                self.refresh_all()
                self._continue_run()
            elif cmd == "change":
                self._log_line(self.engine.request_change(args[0], " ".join(args[1:])))
                self.refresh_all()
            elif cmd == "answer":
                self._log_line(self.ops.answer_all() if args == ["all"] else self.ops.answer(args[0], " ".join(args[1:])))
                self.refresh_all()
            elif cmd == "gate":
                if not args:
                    raise EngineError("usage: /gate <step> [human|auto|auto_answer|none] [--session]  (no mode: pick one)")
                if len(args) == 1 or args[1].startswith("--"):
                    self.action_gate_mode(args[0])
                else:
                    self.log_info(self.ops.set_gate_mode(args[0], args[1], save="--session" not in args))
                    self.refresh_all()
            elif cmd == "skill":
                self.action_skill_editor()
            elif cmd == "prompts":
                self.action_prompt_editor()
            elif cmd == "flow":
                self.action_flow_editor()
            elif cmd == "step":
                if len(args) < 2:
                    raise EngineError("usage: /step <step> view|pass|notes [text] [--session]  (no text: clear; pass conditions split by ;;)")
                text = " ".join(a for a in args[2:] if a != "--session")
                self.log_info(self.ops.set_step_setting(args[0], args[1], text, save="--session" not in args))
                self.refresh_all()
            elif cmd == "gates":
                for line in self.ops.gates_text().splitlines():
                    self._log_line(line)
            elif cmd == "status":
                for v in self.engine.status():
                    self._log_line(f"{v.name:8} {v.status:13} review={v.gate:8} questions={v.open_questions} {v.detail}")
            elif cmd == "open":
                path = (self.project.root / args[0]).resolve()
                if not path.is_file():
                    raise EngineError(f"no such file: {args[0]}")
                owner = next((st.name for st in self.engine.steps if path in st.outputs(self.engine) + st.inputs(self.engine)), self.selected_step)
                self._select_step(owner)
                panel = self.current_panel()
                panel.show_tab("files")
                panel.query_one("FileViewer").show(path)
            elif cmd == "model":
                if not args:  # model, effort and per-step models together: the settings' LLM tab
                    self.action_settings("llm")
                else:
                    self.set_model(args[0], save="--save" in args)
            elif cmd == "effort":
                if not args or args[0] not in [*EFFORTS, "none"]:
                    raise EngineError(f"usage: /effort {'|'.join(EFFORTS)}|none [--save]")
                self.set_model(effort=None if args[0] == "none" else args[0], save="--save" in args)
            elif cmd in ("autoapprove", "autoanswer", "autoconfirm"):
                if not args or args[0] not in ("on", "off"):
                    raise EngineError(f"usage: /{cmd} on|off [--save]")
                fn = {"autoapprove": self.ops.set_auto_approve,
                      "autoanswer": self.ops.set_auto_answer, "autoconfirm": self.ops.set_auto_confirm_reviews}[cmd]
                fn(args[0] == "on", save="--save" in args)
                self._render_header()
            elif cmd == "specreview":
                if not args or args[0] not in ("on", "off"):
                    raise EngineError(f"usage: /specreview on|off [--save]  (now: {'on' if self.project.cfg.spec.self_review else 'off'})")
                self.ops.set_spec_review(args[0] == "on", save="--save" in args)
            elif cmd == "parallel":
                if not args or args[0] not in ("on", "off"):
                    raise EngineError("usage: /parallel on|off [--multi-agent|--shared] [--save]")
                multi_agent = True if "--multi-agent" in args else (False if "--shared" in args else None)
                self.ops.set_parallel_rtl_tb(args[0] == "on", multi_agent=multi_agent, save="--save" in args)
            elif cmd == "cost":
                if args and args[0] == "reset":
                    self.ops.reset_cost()
                    self._render_header()
                else:
                    self._log_line(f"LLM cost: {self.ops.cost_text()} · /cost reset to clear")
            elif cmd == "reset":
                if not args:
                    raise EngineError("usage: /reset <step>|all [--only]  (R picks interactively; /newchat resets the assistant)")
                self.action_reset_step(args[0], only="--only" in args)
            elif cmd == "stats":
                if args and args[0] == "reset":
                    self._log_line(self.ops.reset_stats())
                else:
                    self.action_stats()
            elif cmd == "settings":
                self.action_settings(args[0] if args else None)
            elif cmd == "newchat":
                self.assistant.reset()
                self._log_line("assistant conversation reset")
            elif cmd in ("quit", "q"):
                self.run_worker(self.action_quit())
            else:
                self.action_help()
        except (EngineError, IndexError) as exc:
            self._log_line(Text(f"{text}: {exc or 'missing argument'}", "red"))
