"""Per-step views for the TUI's lower panel (one per pipeline step, switched with the selection)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical, VerticalScroll
from rich.markdown import Markdown as RichMarkdown
from textual.css.query import NoMatches
from textual.widgets import DataTable, Select, Static, TabbedContent, TabPane, TextArea

from q3tui.core.icons import icon as I
from q3tui.tui import mermaid
from q3tui.tui.i18n import t
from q3tui.tui.splitter import Splitter

if TYPE_CHECKING:
    from q3tui.tui.app import Q3TUIApp


class ResultBanner(Static):
    """One line above a step's view: is this result reviewed, current, edited, yours?"""

    DEFAULT_CSS = """
    ResultBanner { height: auto; padding: 0 1; }
    ResultBanner.warn { background: $warning 20%; }
    ResultBanner.ok { background: $success 15%; }
    ResultBanner.err { background: $error 20%; }
    ResultBanner.info { background: $boost; }
    """


def banner_for(app: "Q3TUIApp", step: str) -> tuple[str, str]:
    """(markup, css class) describing the state of a step's current result."""
    eng = app.engine
    v = eng.evaluate(eng.by_name[step])
    rec = eng.state.steps.get(step)
    gate = eng.state.gates.get(step)
    has_result = any(f.is_file() for f in eng.by_name[step].outputs(eng))
    when = f" · built {rec.finished.replace('T', ' ')}" if rec and rec.finished else ""
    q = f" · {v.open_questions} open question(s) (o)" if v.open_questions else ""
    edited = f"  [b]{I('edited')} {t('banner.edited_by_you')}[/b] {t('banner.edited_detail', files=', '.join(v.edited))}" if v.edited else ""
    if v.status == "running":
        return (f"[b]{I('running')} {t('banner.running')}[/b] — {t('banner.running_detail')}"
                + (f" · {t('banner.showing_previous')}" if has_result else ""), "info")
    if v.status == "failed":
        return (f"[b]{I('failed')} {t('banner.last_run_failed')}[/b] — {v.detail}"
                + (f" · {t('banner.showing_last_good')}" if has_result else "") + f" · {t('banner.retry')}", "err")
    if v.status == "unavailable":
        return (f"[b]{I('planned')} {t('banner.planned')}[/b] ({v.detail})"
                + (f" · {t('banner.showing_your_files')}" if has_result else ""), "info")
    if v.status == "missing_input":
        return (f"[b]? {t('banner.waiting_for_input')}[/b] — {v.detail}", "warn")
    if v.status == "pending":
        return (f"[b]{I('pending')} {t('banner.no_result_yet')}[/b] — {t('banner.press_r_to_run')}"
                + (f" · {t('banner.showing_older_result')}" if has_result else ""), "info")
    if v.status == "stale":
        review = f" · {t('banner.was_approved')}" if gate and gate.status == "approved" else \
            (f" · [b]{I('review')} {t('banner.not_reviewed_short')}[/b]" if gate else "")
        return (f"[b]! {t('banner.out_of_date')}[/b] — {t('banner.inputs_changed')}{when}; "
                f"{t('banner.showing_old_result')} · {t('banner.update_r')}{review}{q}{edited}", "warn")
    if v.status == "user":
        return (f"[b]{I('user')} {t('banner.yours')}[/b] — {t('banner.yours_detail')}{q}{edited}", "ok")
    if v.gate == "open":
        blocking = sum(1 for qq in eng.questions() if qq["step"] == step and qq["status"] == "open" and qq.get("blocking"))
        pending = eng.by_name[step].pending_reviews(eng)
        if blocking:
            return (f"[b]{I('review')} {t('banner.question_waiting')}[/b]{when} — {t('banner.blocking_questions', n=blocking)}{q}{edited}", "warn")
        if pending:
            shown = ", ".join(pending[:3]) + ("…" if len(pending) > 3 else "")
            return (f"[b]{I('review')} {t('banner.confirm_needed')}[/b]{when} — "
                    f"{t('banner.items_not_reviewed', n=len(pending), items=shown)}{q}{edited}", "warn")
        return (f"[b]{I('review')} {t('banner.not_reviewed')}[/b]{when} — {t('banner.review_or_change')}{q}{edited}", "warn")
    approved = f" {gate.approved_at.replace('T', ' ')}" if gate and gate.approved_at else ""
    label = f"{I('approved')} {t('banner.approved')}{approved}" if v.gate == "approved" else f"{I('done')} {t('banner.done')}"
    return (f"[b]{label}[/b]{when}{q}{edited}", "ok")


_FENCE = re.compile(r"^\s*(`{3,}|~{3,})\s*([\w+-]*)")


_BOX = re.compile(r"[├└│┌┐┘┬┴┼]|──")


def fence_diagrams(text: str) -> str:
    """Markdown joins a paragraph's lines into one, which wrecks a tree or diagram written without a code fence: a block of
    lines (outside fences) that holds box-drawing characters on at least two lines is put in a ```text fence."""
    out: list[str] = []
    block: list[str] = []
    in_fence = False

    def flush() -> None:
        if len(block) >= 2 and sum(bool(_BOX.search(ln)) for ln in block) >= 2:
            out.extend(["```text", *block, "```"])
        else:
            out.extend(block)
        block.clear()

    for line in text.split("\n"):
        if _FENCE.match(line):
            flush()
            in_fence = not in_fence
            out.append(line)
        elif in_fence or not line.strip():
            flush()
            out.append(line)
        else:
            block.append(line)
    flush()
    return "\n".join(out)


def split_fences(text: str) -> list[tuple[str, str, str]]:
    """Markdown → [("md", text, ""), ("code", code, lang), ...] at fenced code blocks."""
    parts: list[tuple[str, str, str]] = []
    buf: list[str] = []
    code: list[str] | None = None
    fence = lang = ""
    for line in fence_diagrams(text).split("\n"):
        m = _FENCE.match(line)
        if code is None and m:
            if "\n".join(buf).strip():
                parts.append(("md", "\n".join(buf), ""))
            buf, code, fence, lang = [], [], m.group(1), m.group(2) or "text"
        elif code is not None and m and m.group(1).startswith(fence) and not m.group(2):
            parts.append(("code", "\n".join(code), lang))
            code = None
        elif code is not None:
            code.append(line)
        else:
            buf.append(line)
    if code is not None:
        parts.append(("code", "\n".join(code), lang))
    if "\n".join(buf).strip():
        parts.append(("md", "\n".join(buf), ""))
    return parts


class MdCode(ScrollableContainer):
    """A fenced code block: never wrapped (diagrams, waveforms, SV); scrolls sideways when wide."""

    DEFAULT_CSS = """
    MdCode { height: auto; overflow-x: auto; overflow-y: hidden; margin: 0 0 1 0; background: $boost; }
    MdCode > Static { width: auto; padding: 0 1; }
    """

    def __init__(self, code: str, lang: str):
        super().__init__()
        self._static = Static(self.renderable(code, lang))

    @staticmethod
    def renderable(code: str, lang: str):
        if lang.lower() in mermaid.LANGS:  # a diagram: drawn as terminal art (its source when it cannot be drawn)
            art = mermaid.render(code)
            if art is not None:
                return Text(art, no_wrap=True, overflow="ignore")
            return Text(code, no_wrap=True, overflow="ignore")
        if lang in ("", "text", "txt", "plain"):
            return Text(code, no_wrap=True, overflow="ignore")
        from rich.syntax import Syntax

        return Syntax(code, lang, theme="ansi_dark", word_wrap=False, background_color="default")

    def compose(self) -> ComposeResult:
        yield self._static

    def set(self, code: str, lang: str) -> None:
        self._static.update(self.renderable(code, lang))


class MdProse(Static):
    """A run of markdown prose, rendered by Rich into styled text at the widget's width, so that it can be selected with the
    mouse (a Rich renderable cannot be) — re-rendered when the width changes. See Q3TUIApp.action_comment."""

    def __init__(self, body: str):
        super().__init__("", classes="md-prose")
        self.body = body
        self._width = 0

    def set(self, body: str) -> None:
        if body != self.body:
            self.body, self._width = body, 0
            self._rebuild()

    def on_resize(self, event) -> None:
        self._rebuild()

    def _rebuild(self) -> None:
        width = self.size.width
        if width <= 0 or width == self._width:
            return
        from io import StringIO

        from rich.console import Console

        self._width = width
        console = Console(width=width, file=StringIO(), force_terminal=True, color_system="truecolor")
        lines = console.render_lines(RichMarkdown(self.body, code_theme="ansi_dark", hyperlinks=False),
                                     console.options.update(width=width), pad=False)
        out = Text()
        for i, line in enumerate(lines):
            if i:
                out.append("\n")
            for seg in line:
                out.append(seg.text, seg.style)
        self.update(out)


class Markdown(Vertical):
    """Markdown rendered by Rich: one Static per prose run, one MdCode per fenced code block.

    Textual's Markdown widget mounts a widget per block (thousands for a 30 KB spec), which
    made every refresh and view switch slow. Rich's own code blocks always word-wrap, which
    folds wide diagrams; so code blocks are split out (a document has only a few).
    """

    DEFAULT_CSS = "Markdown { height: auto; padding: 0 1; } Markdown > .md-prose { height: auto; }"

    def __init__(self, text: str = "", **kw):
        super().__init__(**kw)
        self.source = text
        self._kinds: list[str] = []
        self._parts: list = []

    @staticmethod
    def _make(part: tuple[str, str, str]):
        kind, body, lang = part
        if kind == "code":
            return MdCode(body, lang)
        return MdProse(body)

    def compose(self) -> ComposeResult:
        parts = split_fences(self.source)
        self._kinds = [p[0] for p in parts]
        self._parts = [self._make(p) for p in parts]
        yield from self._parts

    def update_md(self, text: str) -> None:
        if text == self.source:
            return
        self.source = text
        if not self.is_mounted:
            return  # compose() renders self.source
        parts = split_fences(text)
        kinds = [p[0] for p in parts]
        if kinds == self._kinds:  # same shape: update in place (no remounting)
            for widget, (kind, body, lang) in zip(self._parts, parts):
                if kind == "code":
                    widget.set(body, lang)
                else:
                    widget.set(body)
            return
        self._kinds = kinds
        self.remove_children()
        self._parts = [self._make(p) for p in parts]
        if self._parts:
            self.mount_all(self._parts)


def set_markdown(widget: Markdown, text: str) -> None:
    widget.update_md(text)


class StepPanel(Vertical):
    """Base: a view bound to one pipeline step."""

    step_name = ""
    main_tab = ""

    @property
    def capp(self) -> "Q3TUIApp":
        return self.app  # type: ignore[return-value]

    def _parts(self) -> tuple:
        """(banner, [file viewers + question tables]) — looked up once; DOM queries are slow
        once big Markdown documents are mounted."""
        if not hasattr(self, "_cached_parts"):
            banners = list(self.query(ResultBanner))
            self._cached_parts = (banners[0] if banners else None, list(self.query("FileViewer, QuestionsTable, .refreshable")))
        return self._cached_parts

    def refresh_view(self) -> None:  # called after pipeline changes
        if not self.is_mounted:
            return
        banner, children = self._parts()
        if banner is not None:
            text, cls = banner_for(self.capp, self.step_name)
            if getattr(banner, "_last", None) != (text, cls):
                banner._last = (text, cls)  # type: ignore[attr-defined]
                banner.update(text)
                banner.set_classes(cls)
        for child in children:
            if child.is_mounted:
                try:
                    child.refresh_view()  # type: ignore[attr-defined]
                except NoMatches:  # widget tree still being (un)mounted; next refresh catches up
                    pass

    def show_tab(self, tab: str) -> bool:
        tabs = self.query(TabbedContent)
        if tabs and tabs.first().query(f"#{self.step_name}-{tab}"):
            tabs.first().active = f"{self.step_name}-{tab}"
            return True
        return False

    def current_file(self) -> Path | None:
        viewers = self.query(FileViewer)
        return viewers.first().current if viewers else None


# -- shared widgets -------------------------------------------------------------------------


class FileViewer(Vertical):
    """File picker + viewer (Markdown rendered, everything else as text) for one step's files."""

    DEFAULT_CSS = """
    FileViewer > Select { margin: 0 0 1 0; }
    FileViewer > VerticalScroll, FileViewer > TextArea { height: 1fr; }
    """

    def __init__(self, step: str, extra=None, **kw):
        super().__init__(**kw)
        self.step, self.extra = step, extra
        self.current: Path | None = None

    def compose(self) -> ComposeResult:
        yield Select([], prompt="select a file", allow_blank=True)
        with VerticalScroll():
            yield Markdown()
        yield TextArea(read_only=True, show_line_numbers=True)

    def on_mount(self) -> None:
        self.query_one(TextArea).display = False

    def files(self) -> list[Path]:
        app: Q3TUIApp = self.app  # type: ignore[assignment]
        step = app.engine.by_name[self.step]
        files = step.outputs(app.engine) + step.inputs(app.engine) + (self.extra(app) if self.extra else []) + app.engine.view_files(self.step)
        out: list[Path] = []
        for f in files:
            if f.is_file() and f not in out:
                out.append(f)
        return out

    def refresh_view(self) -> None:
        app: Q3TUIApp = self.app  # type: ignore[assignment]
        select = self.query_one(Select)
        options = [(app.project.rel(f), str(f)) for f in self.files()]
        current = select.value
        if getattr(self, "_options", None) != options:
            self._options = options
            select.set_options(options)
        values = [v for _, v in options]
        if isinstance(current, str) and current in values:
            select.value = current
            self.show(Path(current))  # re-read: the file may have changed
        elif values:
            select.value = values[0]
            self.show(Path(values[0]))
        else:
            self.clear()

    def clear(self) -> None:
        """No files (e.g. after a reset): never keep showing a deleted file's content."""
        self.current = None
        self._shown_key = None
        md, text, wrap = self.query_one(Markdown), self.query_one(TextArea), self.query_one(VerticalScroll)
        set_markdown(md, "_No files for this step yet._")
        text.load_text("")
        wrap.display, text.display = True, False

    def on_select_changed(self, event: Select.Changed) -> None:
        if isinstance(event.value, str):
            self.show(Path(event.value))

    def show(self, path: Path) -> None:
        if not path.is_file():
            self.clear()
            return
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
        if getattr(self, "_shown_key", None) == key:
            return  # already showing this exact content
        self._shown_key = key
        self.current = path
        md, text, wrap = self.query_one(Markdown), self.query_one(TextArea), self.query_one(VerticalScroll)
        try:
            content = path.read_text(errors="replace")
        except OSError as exc:
            content = f"cannot read {path}: {exc}"
        if path.suffix == ".md":
            set_markdown(md, content)
            wrap.display, text.display = True, False
        else:
            text.load_text(content)
            wrap.display, text.display = False, True


def fill_table(table: DataTable, rows: list[tuple]) -> bool:
    """Replace a table's rows only if they changed (keeps cursor and avoids re-render). rows: (key, *cells)."""
    seen: dict[str, int] = {}
    unique = []
    for r in rows:  # (a table row key must be unique: a repeated one — two assertions of the same name — must not crash the UI)
        n = seen.get(r[0], 0)
        seen[r[0]] = n + 1
        unique.append(r if n == 0 else (f"{r[0]}#{n + 1}", *r[1:]))
    rows = unique
    sig = [(r[0], tuple(str(c) for c in r[1:])) for r in rows]
    if getattr(table, "_q3tui_rows", None) == sig:
        return False
    before = {k for k, _ in getattr(table, "_q3tui_rows", None) or []}
    table._q3tui_rows = sig  # type: ignore[attr-defined]
    cursor = table.cursor_row
    table.clear()
    for key, *cells in rows:
        table.add_row(*cells, key=key)
    if table.row_count:
        added = [i for i, (k, _) in enumerate(sig) if k not in before] if before else []
        # something new appeared (added by you or the assistant): select it and scroll it into view
        target = added[-1] if added else min(max(cursor, 0), table.row_count - 1)
        table.move_cursor(row=target, scroll=True)
        # a row wider than the pane makes the table scroll sideways to "show" it, hiding the
        # first (id) column — keep the view pinned to the left
        table.call_after_refresh(lambda: table.scroll_to(x=0, animate=False))
    return True


def short(text: str, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


class IdTable(DataTable):
    """Row table that keeps its first (id) column visible and lets End/Home jump to the last/first row."""

    BINDINGS = [Binding("end", "last_row", "Last", show=False), Binding("home", "first_row", "First", show=False)]

    def action_last_row(self) -> None:
        if self.row_count:
            self.move_cursor(row=self.row_count - 1, scroll=True)
            self.call_after_refresh(lambda: self.scroll_to(x=0, animate=False))

    def action_first_row(self) -> None:
        if self.row_count:
            self.move_cursor(row=0, scroll=True)
            self.call_after_refresh(lambda: self.scroll_to(x=0, animate=False))

    def on_show(self) -> None:
        """Shown again (view/tab switch): bring the selected row back into view."""
        if self.row_count:
            self.call_after_refresh(lambda: (self.move_cursor(row=self.cursor_row, scroll=True), self.scroll_to(x=0, animate=False)))

    def watch_cursor_coordinate(self, old, new) -> None:  # noqa: D401 - Textual watcher
        super().watch_cursor_coordinate(old, new)
        self.call_after_refresh(lambda: self.scroll_to(x=0, animate=False))


class QuestionsTable(Vertical):
    """Questions of one step: open first, then closed (answered / default accepted). Enter answers."""

    BINDINGS = [Binding("h", "toggle_closed", t("q.toggle_closed"))]

    def __init__(self, step: str, **kw):
        super().__init__(**kw)
        self.step = step
        self.show_closed = True

    def compose(self) -> ComposeResult:
        yield Static("", classes="hint")
        yield DataTable(cursor_type="row", zebra_stripes=True)

    def on_mount(self) -> None:
        self.query_one(DataTable).add_columns(t("q.col_id"), t("q.col_status"), t("q.col_question"), t("q.col_answer"))

    def refresh_view(self) -> None:
        app: Q3TUIApp = self.app  # type: ignore[assignment]
        table = self.query_one(DataTable)
        rows = []
        qs = [q for q in app.engine.questions() if q["step"] == self.step]
        open_q = [q for q in qs if q["status"] == "open"]
        closed = [q for q in qs if q["status"] != "open"]
        self.query_one(Static).update(
            t("q.summary", open=len(open_q), closed=len(closed), hidden="" if self.show_closed else t("q.hidden_suffix"))
            + ("" if self.step == "spec" else t("q.target_suffix"))
        )
        for q in open_q + (closed if self.show_closed else []):
            if q["status"] == "open":
                status = Text(f"{I('blocking')} {t('q.blocking')}", "red") if q.get("blocking") else Text(f"{I('open')} {t('q.open')}", "yellow")
                ans = Text(q.get("default_assumption") or "", "grey50")
                qstyle = ""
            else:
                status = Text(f"{I('answered')} {t('q.answered')}", "green") if q["status"] == "answered" else Text(f"{I('answered')} {t('q.default')}", "cyan")
                ans = Text(q.get("answer") or "", "green" if q["status"] == "answered" else "cyan")
                qstyle = "grey62"
                if q.get("history"):
                    status.append(t("q.old"), "grey42")
            flat = " ".join(q["question"].split())            # one table line (answers may have line breaks)
            question = flat if len(flat) <= 80 else flat[:79] + "…"
            plain = " ".join(ans.plain.split())
            answer = plain if len(plain) <= 50 else plain[:49] + "…"
            qtext = Text(question, qstyle)
            target = self.app.engine.gap_target(self.step, q)  # type: ignore[attr-defined]
            if target:  # the answer is handed to that earlier step
                qtext = Text.assemble((f"→ {target} ", "magenta"), qtext)
            rows.append((q["id"], q["id"], status, qtext, Text(answer, ans.style)))
        fill_table(table, rows)

    def action_toggle_closed(self) -> None:
        self.show_closed = not self.show_closed
        self.refresh_view()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        self.app.answer_question(event.row_key.value)  # type: ignore[attr-defined]


# -- step 1: spec -------------------------------------------------------------------------------


class SectionsPanel(Horizontal):
    """Template sections next to their content in spec.md; edit the template in place."""

    BINDINGS = [
        Binding("n", "add", "Add section"),
        Binding("d", "delete", "Delete"),
        Binding("t", "toggle", "Required/optional"),
        Binding("left_square_bracket", "move(-1)", "Move up"),
        Binding("right_square_bracket", "move(1)", "Move down"),
        Binding("v", "review", "Mark reviewed"),
        Binding("w", "feedback", "Write feedback"),
        Binding("ctrl+s", "send_feedback", "Send feedback"),
        Binding("escape", "leave_feedback", "Back to sections", show=False),
        Binding("E", "edit_template", "Edit template"),
    ]
    DEFAULT_CSS = """
    SectionsPanel > Vertical#spec-sections-left { width: 72; }
    SectionsPanel DataTable { height: 1fr; }
    SectionsPanel > #spec-section-right { width: 1fr; }
    SectionsPanel #spec-section-right > VerticalScroll { height: 1fr; padding: 0 1; }
    SectionsPanel #spec-feedback { height: auto; max-height: 16; border-top: solid $primary; padding: 0 1; }
    SectionsPanel #spec-feedback-title { text-style: bold; }
    SectionsPanel #spec-feedback-pending { color: $warning; }
    SectionsPanel #spec-feedback-text { height: 5; }
    SectionsPanel #spec-feedback Horizontal { height: auto; }
    SectionsPanel #spec-feedback Button { margin-right: 1; }
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._fb_sid: str | None = None  # the section the feedback box is about
        self._drafts: dict[str, str] = {}  # unsent feedback per section: it survives moving to another row

    def compose(self) -> ComposeResult:
        from textual.widgets import Button

        with Vertical(id="spec-sections-left"):
            yield IdTable(cursor_type="row", zebra_stripes=True, id="spec-sections-table")
            yield Static("Enter edit · v reviewed · w feedback · C comment · n add · d delete · t required · [ ] move · E template", classes="hint")
        yield Splitter(vertical=True, id="split-sections")
        with Vertical(id="spec-section-right"):
            with VerticalScroll():
                yield Markdown(id="spec-section-md")
            with Vertical(id="spec-feedback"):  # feedback for the highlighted section: queued as a change request scoped to it
                yield Static("", id="spec-feedback-title")
                yield Static("", id="spec-feedback-pending")
                yield TextArea("", id="spec-feedback-text", soft_wrap=True, show_line_numbers=False)
                with Horizontal():
                    yield Button("Send feedback (ctrl+s)", id="spec-feedback-send", variant="primary")
                    yield Button("Clear", id="spec-feedback-clear")

    def on_mount(self) -> None:
        self.query_one(DataTable).add_columns("#", "Section", "", "Status")

    @property
    def capp(self) -> "Q3TUIApp":
        return self.app  # type: ignore[return-value]

    def _spec_sections(self) -> dict[str, tuple[str, str]]:
        from q3tui.steps.spec.document import parse_sections

        path = self.capp.project.spec_dir / "spec.md"
        if not path.is_file():
            return {}
        return {sid: (title, body) for sid, title, body in parse_sections(path.read_text())}

    def refresh_view(self) -> None:
        from q3tui.steps.spec.template import load_template

        table = self.query_one(DataTable)
        try:
            load_template(self.capp.project.spec_dir, self.capp.engine.spec_template())
        except ValueError as exc:
            set_markdown(self.query_one(Markdown), f"**Template error:** {exc}")
            return
        from q3tui.steps.spec.sections import section_states

        rows, n = [], 0
        for st in section_states(self.capp.engine):
            n += 1
            text, style = st.label
            req = Text("extra", "cyan") if st.required is None else Text("req" if st.required else "opt", "white" if st.required else "grey50")
            rows.append((st.id, str(n) if st.required is not None else "+", st.title, req, Text(text, style)))
        fill_table(table, rows)
        if table.row_count:
            self._show(self._selected())

    def comment_target(self) -> tuple[str, str] | None:
        """(what to call it, its text) of the highlighted section, for a comment (Q3TUIApp.action_comment)."""
        sid = self._selected()
        if sid is None:
            return None
        title, body = self._spec_sections().get(sid, (sid, ""))
        return f'`spec/spec.md`, section "{title}" ({sid})', body.strip()

    def _selected(self) -> str | None:
        table = self.query_one(DataTable)
        if not table.row_count:
            return None
        return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value

    def _show(self, sid: str | None) -> None:
        from q3tui.steps.spec.sections import section_states
        from q3tui.steps.spec.template import load_template

        if sid is None:
            return
        template, _ = load_template(self.capp.project.spec_dir, self.capp.engine.spec_template())
        ts = template.by_id().get(sid)
        written = self._spec_sections().get(sid)
        st = next((x for x in section_states(self.capp.engine) if x.id == sid), None)
        parts = []
        if st:
            text, _ = st.label
            hints = []
            if st.in_spec and not st.reviewed:
                hints.append("v to mark reviewed")
            if st.tbd:
                hints.append("answer its questions (o) or c to decide")
            if not st.in_spec:
                hints.append("written on the next spec run (r)")
            parts.append(f"**{text}**" + (f" — {'; '.join(hints)}" if hints else ""))
        if ts:
            parts.append(f"_{'required' if ts.required else 'optional'} · template guidance:_ {' '.join(ts.guidance.split())}")
            if ts.fields:
                parts.append("_fields:_ " + ", ".join(f.name for f in ts.fields))
        elif written:
            parts.append("_Not in the template (kept as an extra section)._")
        if written:
            parts += [f"## {written[0]}", written[1] or "_(empty)_"]
        set_markdown(self.query_one(Markdown), "\n\n".join(parts))
        self._show_feedback(sid, written[0] if written else (ts.title if ts else sid))

    # -- feedback per section --

    def _pending_feedback(self, title: str) -> list[str]:
        """Change requests already queued for this section (ops.request_change prefixes them `In section '<title>':`)."""
        prefix = f"in section '{title.lower()}':"
        return [f for f in self.capp.engine.state.feedback.get("spec", []) if f.lower().startswith(prefix)]

    def _show_feedback(self, sid: str, title: str) -> None:
        box = self.query_one("#spec-feedback-text", TextArea)
        if self._fb_sid is not None and self._fb_sid != sid:
            self._drafts[self._fb_sid] = box.text
        if self._fb_sid != sid:
            box.load_text(self._drafts.get(sid, ""))
        self._fb_sid = sid
        self.query_one("#spec-feedback-title", Static).update(f"Feedback for “{title}”")
        pending = self._pending_feedback(title)
        self.query_one("#spec-feedback-pending", Static).update(
            "\n".join([f"↻ queued ({len(pending)}), applied on the next spec run (r):", *(f"  · {f.split(':', 1)[1].strip()}" for f in pending)])
            if pending else "")

    def action_feedback(self) -> None:
        if self._selected():
            self.query_one("#spec-feedback-text", TextArea).focus()

    def action_leave_feedback(self) -> None:
        if self.query_one("#spec-feedback-text", TextArea).has_focus:
            self.query_one(DataTable).focus()

    def action_send_feedback(self) -> None:
        sid = self._fb_sid
        box = self.query_one("#spec-feedback-text", TextArea)
        text = box.text.strip()
        if not sid or not text:
            self.capp.notify("write what should change in this section first (w), then ctrl+s", severity="warning")
            return
        title, _ = self._spec_sections().get(sid, (None, ""))
        if title is None:
            title = next((s.title for s in self.capp.ops.template().sections if s.id == sid), sid)
        box.load_text("")
        self._drafts.pop(sid, None)
        self._do(self.capp.ops.request_change, "spec", text, title)
        self.query_one(DataTable).focus()

    def on_button_pressed(self, event) -> None:
        if event.button.id == "spec-feedback-send":
            event.stop()
            self.action_send_feedback()
        elif event.button.id == "spec-feedback-clear":
            event.stop()
            self.query_one("#spec-feedback-text", TextArea).load_text("")
            if self._fb_sid:
                self._drafts.pop(self._fb_sid, None)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        event.stop()
        self._show(event.row_key.value if event.row_key else None)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter: edit this section's text right here (fields table + body, as Markdown)."""
        event.stop()
        from q3tui.tui.screens import EditScreen

        sid = event.row_key.value
        written = self._spec_sections().get(sid)
        if written is None:
            self.capp.notify("this section is not in spec.md yet — run the spec step, or ask the assistant to write it")
            return
        title, body = written

        def done(values: dict | None) -> None:
            if values is not None and values["body"].strip() != body.strip():
                self._do(self.capp.ops.set_section_body, sid, values["body"], title)

        self.capp.push_screen(EditScreen(f"Edit section: {title}", [("body", "Markdown (fields table and text)", body, "text")],
                                         "ctrl+s to save · Esc to cancel · the section shows as ✎ edited; requirements become out of date"), done)

    # -- template edits: same operations the assistant uses (q3tui.core.ops) --

    def _do(self, fn, *args) -> None:
        from q3tui.pipeline.engine import EngineError

        try:
            self.capp.log_info(fn(*args))
        except (EngineError, ValueError) as exc:
            self.capp.notify(str(exc), severity="error")
        self.capp.refresh_all()

    def action_add(self) -> None:
        after = self._selected()

        def got_title(title: str | None) -> None:
            if not title:
                return

            def got_guidance(guidance: str | None) -> None:
                self._do(self.capp.ops.add_section, title, guidance or "", True, None, after)

            self.capp.push_screen(self.capp.prompt_screen(f"Section '{title}': what should it contain?", "Guidance for the spec writer (optional)."), got_guidance)

        self.capp.push_screen(self.capp.prompt_screen("New section title", "Inserted after the selected section.", multiline=False), got_title)

    def action_delete(self) -> None:
        sid = self._selected()
        if sid:
            self._do(self.capp.ops.remove_section, sid)

    def action_toggle(self) -> None:
        sid = self._selected()
        if not sid:
            return
        t = self.capp.ops.template()
        s = t.by_id().get(sid)
        if s is not None:
            self._do(self.capp.ops.update_section, sid, None, None, not s.required)

    def action_move(self, delta: int) -> None:
        sid = self._selected()
        if not sid or sid not in self.capp.ops.template().by_id():
            return
        self._do(self.capp.ops.move_section, sid, None, delta)
        table = self.query_one(DataTable)
        table.move_cursor(row=max(0, min(table.row_count - 1, table.cursor_row + delta)))

    def action_review(self) -> None:
        from q3tui.steps.spec.sections import section_states

        sid = self._selected()
        st = next((x for x in section_states(self.capp.engine) if x.id == sid), None)
        if st is None or not st.in_spec:
            self.capp.notify("this section is not in spec.md yet")
            return
        self._do(self.capp.ops.mark_reviewed, sid, not st.reviewed)
        remaining = [x.title for x in section_states(self.capp.engine) if x.in_spec and not x.reviewed]
        if not st.reviewed and not remaining and self.capp.engine.evaluate(self.capp.engine.by_name["spec"]).gate == "open":
            self.capp.log_info("all sections reviewed — press a to approve the spec")

    def action_edit_template(self) -> None:
        from q3tui.steps.spec.template import ensure_project_template

        self.capp.edit_file(ensure_project_template(self.capp.project.spec_dir, self.capp.engine.spec_template()))


class SpecPanel(StepPanel):
    step_name = "spec"
    main_tab = "sections"

    def compose(self) -> ComposeResult:
        yield ResultBanner()
        with TabbedContent():
            with TabPane("Sections", id="spec-sections"):
                yield SectionsPanel()
            with TabPane("Questions", id="spec-questions"):
                yield QuestionsTable("spec")
            with TabPane("Files", id="spec-files"):
                from q3tui.steps.spec.template import find_template

                yield FileViewer("spec", extra=lambda app: [find_template(app.project.spec_dir, app.engine.spec_template())])

    def refresh_view(self) -> None:
        super().refresh_view()
        if not hasattr(self, "_sections"):
            self._sections = self.query_one(SectionsPanel)
        self._sections.refresh_view()


# -- step 2: model -----------------------------------------------------------------------------
