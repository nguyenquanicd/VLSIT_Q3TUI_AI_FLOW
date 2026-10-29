"""Modal screens: review gate, feedback, answering a question."""

from __future__ import annotations

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static, TextArea

from q3tui.tui.picker import Choice, ChoiceResult, ChoiceScreen  # noqa: F401 - re-exported with the other screens


class GateScreen(ChoiceScreen):
    """Review gate, as a picker: approve / answer questions / edit / request changes / later. The options follow the
    gate's state (Approve is hidden while blocking questions are open); dismisses with the choice's name."""

    DEFAULT_CSS = "GateScreen .modal-box { border: thick $warning; }"

    def __init__(self, step: str, summary: str, mode: str = "human", blocking: int = 0, open_questions: int = 0,
                 label: str | None = None):
        self.step, self.mode, self.blocking, self.open_questions = step, mode, blocking, open_questions
        self.summary = summary
        choices = []
        if not blocking:
            choices.append(Choice("approve", "Approve", "accept this result; the pipeline goes on", recommended=True, key="a", style="green"))
        if open_questions or blocking:
            choices.append(Choice("questions", "Answer questions", f"{open_questions} open" + (f", {blocking} blocking" if blocking else ""),
                                  recommended=bool(blocking), key="q"))
        choices += [Choice("edit", "Edit", "open the result in your editor", key="e"),
                    Choice("reject", "Request changes", "say what should change; the step updates", key="r", style="yellow"),
                    Choice("later", "Later", "leave the review open", key="l")]
        modes = {"human": "you review it", "auto": "auto: approves itself unless a blocking question is open",
                 "auto_answer": "auto-answer: unattended"}
        body = f"{summary}\nGate mode: {mode} ({modes.get(mode, mode)}) · /gate {step} <mode> changes it"
        super().__init__(f"Review: {label or step}", choices, body=body, other=None, markup_body=True)

    def finish(self, result) -> None:
        self.dismiss(result.value if result is not None and result.value else "later")


class TextPromptScreen(ModalScreen[str | None]):
    """Text input: an answer, a change request, guidance (multi-line: Enter = new line, ctrl+s
    submits) or a short value like a title (one line: Enter submits). The body (e.g. the
    question) is shown in full, line breaks kept, scrollable."""

    BINDINGS = [Binding("escape", "cancel", "Cancel"), Binding("ctrl+s", "submit", "Submit")]
    DEFAULT_CSS = """
    TextPromptScreen .modal-box { width: 100; }
    TextPromptScreen #prompt-body { height: auto; max-height: 14; margin: 1 0 0 0; }
    TextPromptScreen #prompt-body > Static { color: $text-muted; }
    TextPromptScreen #prompt-area { height: 10; margin: 1 0 0 0; }
    """

    def __init__(self, title: str, body: str = "", value: str = "", placeholder: str = "", multiline: bool = True):
        super().__init__()
        self.title_text, self.body, self.value, self.placeholder = title, body, value, placeholder
        self.multiline = multiline

    def compose(self) -> ComposeResult:
        from textual.containers import VerticalScroll
        from textual.widgets import TextArea

        with Vertical(classes="modal-box"):
            yield Label(self.title_text, classes="modal-title")
            if self.body:
                with VerticalScroll(id="prompt-body"):
                    yield Static(self.body, markup=False)
            if self.multiline:
                yield TextArea(self.value, id="prompt-area", soft_wrap=True, show_line_numbers=False)
                yield Static("Enter: new line · ctrl+s: submit · Esc: cancel", classes="modal-hint")
            else:
                yield Input(value=self.value, placeholder=self.placeholder, id="prompt-input")
                yield Static("Enter to submit · Esc to cancel", classes="modal-hint")

    def on_mount(self) -> None:
        from textual.widgets import TextArea

        if self.multiline:
            area = self.query_one("#prompt-area", TextArea)
            area.focus()
            area.move_cursor(area.document.end)
        else:
            self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def action_submit(self) -> None:
        from textual.widgets import TextArea

        text = self.query_one("#prompt-area", TextArea).text if self.multiline else self.query_one(Input).value
        self.dismiss(text.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


from q3tui.core.icons import icon  # noqa: E402
from q3tui.core.settings import EFFORTS, MODELS  # noqa: E402 - one list for the dialogs and the assistant

_NONE = "__none__"


class SettingsScreen(ModalScreen[dict | None]):
    """Settings in tabs (General, LLM, one per step), built from q3tui/core/settings.py."""

    BINDINGS = [Binding("escape", "cancel", "Cancel"), Binding("ctrl+s", "apply", "Apply")]
    DEFAULT_CSS = """
    SettingsScreen #settings-box { width: 110; height: 85%; }
    SettingsScreen TabbedContent { height: 1fr; }
    SettingsScreen TabPane { padding: 0 1; }
    SettingsScreen .set-row { height: auto; margin: 1 0 0 0; }
    SettingsScreen .set-label { width: 30; padding: 1 1 0 0; text-style: bold; }
    SettingsScreen .set-row Input, SettingsScreen .set-row Select { width: 1fr; }
    SettingsScreen .set-row SelectionList { width: 1fr; height: auto; max-height: 8; }
    SettingsScreen .set-help { color: $text-muted; padding: 0 0 0 30; }
    SettingsScreen #settings-error { color: $error; height: auto; }
    """

    def __init__(self, values: dict, initial_tab: str | None = None, gates: list[tuple[str, str, str]] | None = None, tabs=None):
        super().__init__()
        self.values, self.initial_tab, self.tabs = values, initial_tab, tabs
        self.gates = gates or []  # (step, title, mode) of the flow's steps: the Gates tab

    def compose(self) -> ComposeResult:
        from textual.containers import VerticalScroll
        from textual.widgets import Checkbox, TabbedContent, TabPane

        from q3tui.core.settings import TABS

        with Vertical(id="settings-box", classes="modal-box"):
            yield Label("Settings", classes="modal-title")
            with TabbedContent(initial=f"set-tab-{self.initial_tab}" if self.initial_tab else ""):
                for tab in self.tabs or TABS:
                    with TabPane(tab.title, id=f"set-tab-{tab.id}"):
                        with VerticalScroll():
                            for st in tab.settings:
                                if st.hidden:
                                    continue
                                with Horizontal(classes="set-row"):
                                    yield Label(st.label, classes="set-label")
                                    yield self._widget(st)
                                if st.help:
                                    yield Static(st.help, classes="set-help", markup=False)
                    if tab.id == "general" and self.gates:
                        yield from self._gates_pane()
            yield Static("", id="settings-error")
            yield Checkbox("Only for this session (don't save to q3tui.yaml)", id="settings-session-only")
            with Horizontal(classes="modal-buttons"):
                yield Button("Apply (ctrl+s)", id="apply", variant="success")
                yield Button("Cancel (esc)", id="cancel")

    def _gates_pane(self):
        """Gates tab: the mode of every step's review gate in this flow."""
        from textual.containers import VerticalScroll
        from textual.widgets import Select, TabPane

        from q3tui.core.settings import GATE_MODES

        with TabPane("Gates", id="set-tab-gates"):
            with VerticalScroll():
                yield Static("How each step's review gate behaves. human: stops for you · auto: approves itself, blocking "
                             "questions still stop it · auto-answer: unattended · none: no gate.", classes="set-help", markup=False)
                for step, title, mode in self.gates:
                    with Horizontal(classes="set-row"):
                        yield Label(f"{title}", classes="set-label")
                        yield Select([(label, v) for v, label in GATE_MODES], value=mode, allow_blank=False, id=self._gate_wid(step))

    @staticmethod
    def _gate_wid(step: str) -> str:
        import re

        return "set-gate-" + re.sub(r"[^A-Za-z0-9_-]", "_", step)

    @staticmethod
    def _wid(path: str) -> str:
        return "set-" + path.replace(".", "-")

    def _widget(self, st):
        from textual.widgets import Select, SelectionList, Switch

        value = self.values.get(st.path)
        wid = self._wid(st.path)
        if st.kind == "bool":
            return Switch(value=bool(value), id=wid)
        if st.kind == "choice":
            options = [(label, v) for v, label in st.choices]
            if value is not None and value not in [v for v, _ in st.choices]:
                options.append((f"current: {value}", value))
            if st.nullable:
                options.append((st.none_label, _NONE))
            return Select(options, value=_NONE if value is None else value, allow_blank=False, id=wid)
        if st.kind == "multi":
            return SelectionList(*[(label, v, v in (value or [])) for v, label in st.choices], id=wid)
        if st.kind == "lines":  # one item per line (e.g. RTL rules)
            from textual.widgets import TextArea

            area = TextArea("\n".join(value or []), id=wid, soft_wrap=True)
            area.styles.height = 12
            return area
        kind = {"int": "integer", "float": "number"}.get(st.kind, "text")
        return Input(value="" if value is None else str(value), type=kind, id=wid,
                     placeholder="empty = none" if st.nullable else "")

    def _by_path(self) -> dict:
        from q3tui.core.settings import TABS

        return {s.path: s for t in (self.tabs or TABS) for s in t.settings}

    def _read(self) -> dict:
        from textual.widgets import Select, SelectionList, Switch

        from q3tui.core.settings import TABS

        out = {}
        for path, st in self._by_path().items():
            if st.hidden:
                out[path] = self.values.get(path)
                continue
            w = self.query_one(f"#{self._wid(path)}")
            if isinstance(w, Switch):
                out[path] = w.value
            elif isinstance(w, Select):
                out[path] = None if w.value == _NONE else w.value
            elif isinstance(w, SelectionList):
                out[path] = list(w.selected)
            elif hasattr(w, "text") and not hasattr(w, "value"):  # TextArea: one item per line
                out[path] = w.text
            else:
                out[path] = w.value
        return out

    def action_apply(self) -> None:
        from textual.widgets import Checkbox

        from q3tui.core.settings import coerce

        raw = self._read()
        try:
            by_path = self._by_path()
            # a field left empty that had no value is unchanged (an option whose default lives elsewhere), not a missing value
            values = {p: None if self.values.get(p) is None and (v is None or (isinstance(v, str) and not v.strip()))
                      else coerce(by_path[p], v) for p, v in raw.items()}
        except ValueError as exc:
            self.query_one("#settings-error", Static).update(str(exc))
            return
        changes = {p: v for p, v in values.items() if v != self.values.get(p)}
        from textual.widgets import Select

        gates = {step: self.query_one(f"#{self._gate_wid(step)}", Select).value for step, _, mode in self.gates
                 if self.query_one(f"#{self._gate_wid(step)}", Select).value != mode}
        self.dismiss({"changes": changes, "gates": gates, "save": not self.query_one("#settings-session-only", Checkbox).value})

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "apply":
            self.action_apply()
        elif event.button.id == "cancel":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ModelScreen(ModalScreen[dict | None]):
    """Pick LLM model + effort; optionally remember in the project's q3tui.yaml."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, model: str, effort: str | None):
        super().__init__()
        self.model, self.effort = model, effort

    def compose(self) -> ComposeResult:
        from textual.widgets import Checkbox, RadioButton, RadioSet

        known = [m for m, _ in MODELS]
        with Vertical(classes="modal-box"):
            yield Label("LLM model", classes="modal-title")
            with RadioSet(id="model-set"):
                for mid, desc in MODELS:
                    yield RadioButton(f"{desc}  [dim]{mid}[/]", value=mid == self.model, name=mid)
                if self.model not in known:
                    yield RadioButton(f"current: {self.model}", value=True, name=self.model)
            yield Input(placeholder="or type another model id", id="model-custom")
            yield Label("Effort", classes="modal-title")
            with RadioSet(id="effort-set"):
                for e in EFFORTS:
                    yield RadioButton(e, value=e == (self.effort or "high"), name=e)
            yield Checkbox("Remember for this project (writes q3tui.yaml)", id="model-save")
            with Horizontal(classes="modal-buttons"):
                yield Button("Apply", id="apply", variant="success")
                yield Button("Cancel", id="cancel")
            yield Static("Applies to the next LLM stage; a running stage keeps its model.", classes="modal-hint")

    def on_mount(self) -> None:
        self.query_one("#model-set").focus()

    def _result(self) -> dict:
        from textual.widgets import Checkbox, RadioSet

        custom = self.query_one("#model-custom", Input).value.strip()
        pressed = self.query_one("#model-set", RadioSet).pressed_button
        model = custom or (pressed.name if pressed else self.model)
        eff_btn = self.query_one("#effort-set", RadioSet).pressed_button
        effort = eff_btn.name if eff_btn else self.effort
        if model.startswith("claude-haiku-4"):
            effort = None
        return {"model": model, "effort": effort, "save": self.query_one("#model-save", Checkbox).value}

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(self._result() if event.button.id == "apply" else None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(self._result())

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmScreen(ChoiceScreen):
    """Yes / no as a picker (y / n keys too). Dismisses with a bool."""

    def __init__(self, question: str, detail: str = ""):
        self.question, self.detail = question, detail
        super().__init__(question, [Choice("yes", "Yes", key="y", style="red"), Choice("no", "No", key="n", recommended=True)],
                         body=detail, other=None)

    def finish(self, result) -> None:
        self.dismiss(bool(result is not None and result.value == "yes"))


class ResetScreen(ChoiceScreen):
    """R: reset the selected step (and the steps after it) or everything. Dismisses with "step", "all" or None."""

    def __init__(self, step: str, step_plan: dict, all_plan: dict, rel):
        self.step, self.step_plan, self.all_plan, self.rel = step, step_plan, all_plan, rel
        super().__init__(
            "Reset", [Choice("step", f"Reset {step}", f"{step} and the steps after it — " + self._describe(step_plan), key="s", style="yellow"),
                      Choice("all", "Reset all", "start the whole design from scratch — " + self._describe(all_plan), key="a", style="red"),
                      Choice("cancel", "Cancel", key="n", recommended=True)],
            body="Your inputs (intent, template, your own files) are kept; deleted files are backed up to .q3tui/bkp/.", other=None)

    def _describe(self, plan: dict) -> str:
        files = [self.rel(f) for fs in plan.values() for f in fs]
        shown = ", ".join(files[:6]) + (f", … ({len(files)} files)" if len(files) > 6 else "")
        return f"steps: {', '.join(plan)}; " + (f"deletes: {shown}" if files else "no generated files, clears review/answer state")

    def finish(self, result) -> None:
        v = result.value if result is not None else None
        self.dismiss(v if v in ("step", "all") else None)


class PickToolsScreen(ModalScreen[list[int] | None]):
    """Add tools: every tool found at a path as a checkbox row (all checked); the highlighted one shows its command and what code doubts about it.

    `items`: [(label, detail)]. ctrl+s dismisses with the indexes checked, Esc with None. Space toggles, `a` checks / unchecks all."""

    BINDINGS = [Binding("ctrl+s", "add", "Add"), Binding("escape", "cancel", "Cancel"), Binding("a", "toggle_all", "All / none")]
    DEFAULT_CSS = """
    PickToolsScreen { align: center middle; }
    PickToolsScreen .modal-box { width: 120; max-height: 90%; }
    PickToolsScreen SelectionList { height: auto; max-height: 16; }
    PickToolsScreen #pick-detail { margin-top: 1; height: auto; max-height: 10; color: $text-muted; }
    """

    def __init__(self, title: str, items: list[tuple[str, str]], hint: str = ""):
        super().__init__()
        self.title_text, self.items, self.hint = title, items, hint

    def compose(self) -> ComposeResult:
        from textual.widgets import SelectionList

        with Vertical(classes="modal-box"):
            yield Label(self.title_text, classes="modal-title")
            yield SelectionList(*[(label, i, True) for i, (label, _d) in enumerate(self.items)], id="pick-list")
            yield Static("", id="pick-detail", markup=False)
            with Horizontal(classes="modal-buttons"):
                yield Button("Add checked (ctrl+s)", id="add", variant="success")
                yield Button("Cancel (esc)", id="cancel")
            yield Static(self.hint or "space checks / unchecks · a all / none · ctrl+s adds the checked · Esc cancels", classes="modal-hint")

    def on_mount(self) -> None:
        from textual.widgets import SelectionList

        sel = self.query_one(SelectionList)
        sel.focus()
        self._show(0)

    def _show(self, i: int | None) -> None:
        self.query_one("#pick-detail", Static).update(self.items[i][1] if i is not None and 0 <= i < len(self.items) else "")

    def on_selection_list_selection_highlighted(self, event) -> None:
        self._show(event.selection_index)

    def action_toggle_all(self) -> None:
        from textual.widgets import SelectionList

        sel = self.query_one(SelectionList)
        (sel.deselect_all if len(sel.selected) == len(self.items) else sel.select_all)()

    def action_add(self) -> None:
        from textual.widgets import SelectionList

        self.dismiss(sorted(self.query_one(SelectionList).selected))

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_add() if event.button.id == "add" else self.action_cancel()


class EditScreen(ModalScreen[dict | None]):
    """Small form: fields are (key, label, value, kind[, options]) with kind line | text | choice.

    ctrl+s saves, Esc cancels. Returns {key: value} for all fields. `buttons`: [(id, label, fn(screen))] extra buttons next to Save (a flow's
    helper that works while the form is open: an estimate, a lookup); `fn` may call `screen.set_status(text)` (also from a worker) to answer in the form.
    `on_change`: {field key: fn(screen, value)} called when a choice field changes — a field that depends on another (a technology, then its
    corners) answers with `screen.set_choices(key, options, value)`.
    """

    BINDINGS = [Binding("ctrl+s", "save", "Save"), Binding("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    EditScreen { align: center middle; }
    EditScreen .modal-box { width: 110; max-height: 90%; }
    EditScreen TextArea { height: 8; }
    EditScreen TextArea.tall { height: 22; }
    EditScreen .field-label { color: $text-muted; margin-top: 1; }
    """

    def __init__(self, title: str, fields: list[tuple], hint: str = "", buttons: list[tuple] | tuple = (), on_change: dict | None = None):
        super().__init__()
        self.title_text, self.fields, self.hint, self.buttons = title, fields, hint, list(buttons)
        self.on_change = dict(on_change or {})

    def compose(self) -> ComposeResult:
        from textual.containers import VerticalScroll
        from textual.widgets import Select, TextArea

        with Vertical(classes="modal-box"):
            yield Label(self.title_text, classes="modal-title")
            with VerticalScroll():
                for key, label, value, kind, *rest in self.fields:
                    yield Label(label, classes="field-label")
                    if kind == "text":
                        yield TextArea(value or "", id=f"f-{key}", soft_wrap=True, classes="tall" if len(self.fields) == 1 else "")
                    elif kind == "choice":
                        options = rest[0]
                        yield Select([(o, o) for o in options], value=value if value in options else options[0],
                                     allow_blank=False, id=f"f-{key}")
                    else:
                        yield Input(value or "", id=f"f-{key}")
            with Horizontal(classes="modal-buttons"):
                yield Button("Save (ctrl+s)", id="save", variant="success")
                yield Button("Cancel (esc)", id="cancel")
                for bid, label, _fn in self.buttons:
                    yield Button(label, id=bid)
            if self.buttons:
                yield Static("", id="edit-status", markup=False)
            yield Static(self.hint or "ctrl+s to save · Esc to cancel", classes="modal-hint")

    def on_mount(self) -> None:
        first = self.query("Input, TextArea, Select").first()
        if first is not None:
            first.focus()

    def _values(self) -> dict:
        from textual.widgets import Select, TextArea

        out = {}
        for key, _label, _value, kind, *_ in self.fields:
            w = self.query_one(f"#f-{key}")
            out[key] = w.text if isinstance(w, TextArea) else (w.value if isinstance(w, (Input, Select)) else None)
        return out

    def set_value(self, key: str, value: str) -> None:
        """Put a text into an Input / TextArea field (a helper button fills the form: a scan names the technology)."""
        from textual.widgets import TextArea

        w = self.query_one(f"#f-{key}")
        if isinstance(w, TextArea):
            w.text = value
        elif isinstance(w, Input):
            w.value = value

    def set_choices(self, key: str, options: list[str], value: str | None = None) -> None:
        """Replace the options of a choice field (and pick `value`, else the first)."""
        from textual.widgets import Select

        sel = self.query_one(f"#f-{key}", Select)
        sel.set_options([(o, o) for o in options])
        sel.value = value if value in options else options[0]

    def on_select_changed(self, event) -> None:
        fn = self.on_change.get((event.select.id or "")[2:])
        if fn is not None and event.value is not None and event.value != getattr(event.select, "_last_notified", None):
            event.select._last_notified = event.value  # (a repopulated field changes too: a callback runs once per value)
            fn(self, str(event.value))

    def set_status(self, text: str) -> None:
        """Answer in the form (the status line under the buttons); a no-op once the form is closed."""
        if self.is_attached:
            self.query_one("#edit-status", Static).update(text)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        extra = next((fn for bid, _l, fn in self.buttons if bid == event.button.id), None)
        if extra is not None:
            event.stop()
            extra(self)
            return
        self.dismiss(self._values() if event.button.id == "save" else None)

    def action_save(self) -> None:
        self.dismiss(self._values())

    def action_cancel(self) -> None:
        self.dismiss(None)


class HistoryInput(TextArea):
    """Command line / chat box: Enter sends, Ctrl+J (or Shift+Enter) starts a new line, the box grows with its text. A large paste
    shows as `[+text N lines]` and is sent in full. Shell-like history: ↑ on the first line / ↓ on the last recall earlier entries
    (persisted per project); Tab or → accepts a slash-command completion."""

    BINDINGS = [Binding("up", "history(-1)", "Previous", show=False), Binding("down", "history(1)", "Next", show=False),
                Binding("tab", "complete", "Complete", show=False), Binding("ctrl+j,shift+enter", "newline", "New line", show=False)]
    MAX = 200
    MAX_PASTED = 20          # entries whose pastes the history keeps (older ones are dropped: a paste can be long)
    PASTE_LINES, PASTE_CHARS = 5, 1000   # a paste above either is shown as a placeholder

    class Submitted(Message):
        """Enter: `value` is the text to act on (pastes in full), `shown` what the box showed, `pastes` its placeholders' texts."""

        def __init__(self, box: HistoryInput, value: str, shown: str, pastes: dict[str, str]) -> None:
            super().__init__()
            self.input, self.value, self.shown, self.pastes = box, value, shown, pastes

        @property
        def control(self) -> HistoryInput:
            return self.input

    def __init__(self, history_file=None, completions=None, **kw):
        kw.setdefault("soft_wrap", True)
        kw.setdefault("highlight_cursor_line", False)
        super().__init__(**kw)
        self.entries_file = history_file
        self.completions = completions  # (kind) -> candidates: slash-command completion, as the hint line under the views
        self.pastes: dict[str, str] = {}  # placeholder -> the pasted text
        self.entries: list[str | dict] = self._load()  # (not `history`: TextArea's undo)
        self._pos = len(self.entries)  # len(entries) = the current draft
        self._draft: tuple[str, dict[str, str]] = ("", {})

    @property
    def value(self) -> str:
        return self.text

    @value.setter
    def value(self, text: str) -> None:
        """Replace the text (cursor at its end); pastes whose placeholder is gone are forgotten."""
        self.pastes = {k: v for k, v in self.pastes.items() if k in text}
        self.load_text(text)
        self.move_cursor(self.document.end)

    def expanded(self, text: str | None = None) -> str:
        """The text with each placeholder replaced by its paste."""
        text = self.text if text is None else text
        for token, pasted in self.pastes.items():
            text = text.replace(token, pasted)
        return text

    def _load(self) -> list[str | dict]:
        import json

        try:
            data = json.loads(self.entries_file.read_text()) if self.entries_file and self.entries_file.is_file() else []
            return [x if isinstance(x, dict) else str(x) for x in data if isinstance(x, (str, dict))][-self.MAX:]
        except (OSError, ValueError):
            return []

    def remember(self, text: str, pastes: dict[str, str] | None = None) -> None:
        import json

        text = text.strip()
        entry: str | dict = {"text": text, "pastes": pastes} if pastes else text
        if text and (not self.entries or self.entries[-1] != entry):
            self.entries.append(entry)
            self.entries = self.entries[-self.MAX:]
            pasted = [i for i, e in enumerate(self.entries) if isinstance(e, dict)]
            drop = set(pasted[:-self.MAX_PASTED])
            self.entries = [e for i, e in enumerate(self.entries) if i not in drop]
            if self.entries_file:
                self.entries_file.parent.mkdir(parents=True, exist_ok=True)
                self.entries_file.write_text(json.dumps(self.entries, indent=0))
        self._pos, self._draft = len(self.entries), ("", {})

    def action_submit(self) -> None:
        shown = self.text
        used = {k: v for k, v in self.pastes.items() if k in shown}
        self.post_message(self.Submitted(self, self.expanded(shown), shown, used))

    def action_newline(self) -> None:
        self.insert("\n")

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":  # (TextArea would insert a line break: here Enter sends)
            event.stop()
            event.prevent_default()
            self.action_submit()

    async def _on_paste(self, event: events.Paste) -> None:
        """The terminal's paste (Ctrl+Shift+V, right click, middle click: bracketed paste)."""
        event.prevent_default()
        self.paste(event.text)
        self.focus()

    def action_paste(self) -> None:
        """Ctrl+V: the terminal sends it as a key, so only text copied in Q3TUI is there to paste."""
        if self.app.clipboard:
            self.paste(self.app.clipboard)
        else:
            self.notify("Ctrl+V pastes only text copied in Q3TUI: paste from elsewhere with your terminal's paste (Ctrl+Shift+V)",
                        severity="warning")

    def paste(self, text: str) -> None:
        """Insert pasted text at the cursor (terminal line ends made \\n); a large one as a `[+text N lines]` placeholder."""
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = text.count("\n") + 1
        if self.read_only or not text:
            return
        if lines > self.PASTE_LINES or len(text) > self.PASTE_CHARS:
            label = f"{lines} lines" if lines > 1 else f"{len(text)} chars"
            token, n = f"[+text {label}]", 1
            while token in self.pastes or token in self.text:
                n += 1
                token = f"[+text {label} #{n}]"
            self.pastes[token] = text
            text = token
        self.move_cursor(self.replace(text, *self.selection, maintain_selection_offset=False).end_location)

    def update_suggestion(self) -> None:
        """Ghost-text completion of a slash command, shown at the end of a one-line text."""
        from q3tui.tui.commands import complete

        text, suggestion = self.text, ""
        if self.completions and text.startswith("/") and "\n" not in text and self.selection.is_empty and self.cursor_location == self.document.end:
            full = complete(text, self.completions) or ""
            suggestion = full[len(text):] if full.startswith(text) else ""
        self.suggestion = suggestion

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        self.update_suggestion()

    def on_text_area_selection_changed(self, event: TextArea.SelectionChanged) -> None:
        self.update_suggestion()

    def action_complete(self) -> None:
        """Tab accepts the completion (like →); without one it moves focus as usual."""
        if self.suggestion:
            self.insert(self.suggestion)
        else:
            self.screen.focus_next()

    def action_history(self, step: int) -> None:
        row = self.cursor_location[0]
        if (step < 0 and row > 0) or (step > 0 and row < self.document.line_count - 1):  # inside a multi-line text: move the cursor
            (self.action_cursor_up if step < 0 else self.action_cursor_down)()
            return
        if not self.entries:
            return
        if self._pos == len(self.entries):
            self._draft = (self.text, dict(self.pastes))  # keep what was being typed
        self._pos = max(0, min(len(self.entries), self._pos + step))
        entry = self._draft if self._pos == len(self.entries) else self.entries[self._pos]
        if isinstance(entry, tuple):
            text, pastes = entry
        elif isinstance(entry, dict):
            text, pastes = str(entry.get("text", "")), dict(entry.get("pastes") or {})
        else:
            text, pastes = entry, {}
        self.pastes = pastes
        self.value = text


class StatsScreen(ModalScreen[None]):
    """Usage statistics: tokens per model / step / stage, cost, LLM time, step wall time."""

    BINDINGS = [Binding("escape,q", "close", "Close"), Binding("t", "toggle_scope", "All time / this session"),
                Binding("R", "reset", "Reset")]
    DEFAULT_CSS = """
    StatsScreen #stats-box { width: 140; height: 90%; }
    StatsScreen TabbedContent { height: 1fr; }
    StatsScreen DataTable { height: 1fr; }
    StatsScreen #stats-overview { padding: 1 2; }
    """

    def __init__(self, load, session_start: str, step_order: list[str], reset=None):
        super().__init__()
        self.load, self.session_start, self.step_order, self.reset = load, session_start, step_order, reset
        self.session_only = False

    def compose(self) -> ComposeResult:
        from textual.containers import VerticalScroll
        from textual.widgets import DataTable, TabbedContent, TabPane

        with Vertical(id="stats-box", classes="modal-box"):
            yield Label("Statistics", classes="modal-title", id="stats-title")
            with TabbedContent():
                with TabPane("Overview", id="stats-tab-overview"):
                    with VerticalScroll():
                        yield Static(id="stats-overview")
                for tab in ("model", "step", "stage", "recent"):
                    with TabPane({"model": "By model", "step": "By step", "stage": "By stage", "recent": "Recent stages"}[tab],
                                 id=f"stats-tab-{tab}"):
                        yield DataTable(id=f"stats-{tab}", zebra_stripes=True, cursor_type="row")
            yield Static("t: all time / this session · R: reset · Esc: close", classes="modal-hint")

    def on_mount(self) -> None:
        self._fill()

    def action_toggle_scope(self) -> None:
        self.session_only = not self.session_only
        self._fill()

    def action_reset(self) -> None:
        if self.reset is None:
            return

        def done(ok: bool | None) -> None:
            if ok:
                self.reset()
                self._fill()

        self.app.push_screen(ConfirmScreen("Reset the usage statistics?", "Counting starts from now; the run logs are kept."), done)

    def action_close(self) -> None:
        self.dismiss(None)

    def _fill(self) -> None:
        from rich.table import Table
        from textual.widgets import DataTable

        from q3tui.core.stats import duration, tokens

        s = self.summary = self.load(self.session_start if self.session_only else None)
        t = s.total
        scope = "this session" if self.session_only else "all time" + (f" (since {s.first[:16].replace('T', ' ')})" if s.first else "")
        self.query_one("#stats-title", Label).update(f"Statistics — {scope}")

        grid = Table.grid(padding=(0, 3))
        grid.add_column(style="bold")
        grid.add_column(justify="right")
        grid.add_column(style="dim")
        hit = t.tokens["cache_read"] / t.total_in if t.total_in else 0
        rows = [
            ("Cost", f"${t.cost:.2f}", f"{len(s.by_model)} model(s)"),
            ("LLM stages", str(t.calls), f"{t.turns} turns" + (f", {t.errors} failed" if t.errors else "")),
            ("Input tokens", tokens(t.total_in), f"fresh {tokens(t.tokens['input'])} · cache read {tokens(t.tokens['cache_read'])} "
                                                 f"· cache write {tokens(t.tokens['cache_write'])} · cache hit {hit:.0%}"),
            ("Output tokens", tokens(t.tokens["output"]), f"of which thinking {tokens(t.tokens['thinking'])}"),
            ("LLM time", duration(t.llm_s), f"avg {duration(t.llm_s / t.calls) if t.calls else '—'} per stage"),
            ("Step wall time", duration(t.wall_s), "pipeline steps, start to finish (includes checks and tests)"),
        ]
        for r in rows:
            grid.add_row(*r)
        if not t.calls and not t.wall_s:
            grid.add_row("", "", "no LLM usage recorded yet")
        self.query_one("#stats-overview", Static).update(grid)

        def fill(tid: str, columns: tuple, data: list[tuple]) -> None:
            table = self.query_one(f"#stats-{tid}", DataTable)
            table.clear(columns=True)
            table.add_columns(*columns)
            for row in data:
                table.add_row(*row)

        fill("model", ("Model", "Stages", "Turns", "In", "Cache read", "Cache write", "Out", "Thinking", "Cost", "LLM time"),
             [(m, str(r.calls), str(r.turns), tokens(r.total_in), tokens(r.tokens["cache_read"]), tokens(r.tokens["cache_write"]),
               tokens(r.tokens["output"]), tokens(r.tokens["thinking"]), f"${r.cost:.2f}", duration(r.llm_s))
              for m, r in sorted(s.by_model.items(), key=lambda kv: -kv[1].cost)])
        order = {name: i for i, name in enumerate(self.step_order)}
        fill("step", ("Step", "Stages", "Turns", "In", "Out", "Thinking", "Cost", "LLM time", "Wall time", "Runs"),
             [(st, str(r.calls), str(r.turns), tokens(r.total_in), tokens(r.tokens["output"]), tokens(r.tokens["thinking"]),
               f"${r.cost:.2f}", duration(r.llm_s), duration(r.wall_s), str(r.runs))
              for st, r in sorted(s.by_step.items(), key=lambda kv: order.get(kv[0], 99))])
        fill("stage", ("Stage", "Calls", "Turns", "In", "Out", "Cost", "LLM time", "Avg time"),
             [(st, str(r.calls), str(r.turns), tokens(r.total_in), tokens(r.tokens["output"]), f"${r.cost:.2f}",
               duration(r.llm_s), duration(r.llm_s / r.calls) if r.calls else "—")
              for st, r in sorted(s.by_stage.items(), key=lambda kv: -kv[1].llm_s)])
        fill("recent", ("When", "Step", "Stage", "Model", "In", "Out", "Cost", "Time", "Turns"),
             [((r.get("ts") or "")[5:16].replace("T", " "), r.get("step") or "", r.get("stage") or "", r.get("model") or "",
               tokens(sum(u.get("input", 0) + u.get("cache_read", 0) + u.get("cache_write", 0) for u in r["usage"].values())),
               tokens(sum(u.get("output", 0) for u in r["usage"].values())), f"${r.get('cost', 0):.3f}",
               duration(r.get("duration_ms", 0) / 1000), str(r.get("turns", 0)) + (f" {icon('fail')}" if r.get("error") else ""))
              for r in s.recent])


class FlowScreen(ModalScreen[dict | None]):
    """Flow editor: add / remove / reorder steps, and edit each step's dependencies, gate, view globs, pass conditions and notes.

    rows: [{step, title (shown), kind, deps, gate (mode | None), view, pass, notes, + whatever the app keeps (label, options…)}].
    ctrl+s returns {"changes": {step: {key: value}} (settings of existing steps), "structure": the new row list when steps were
    added / removed / moved / rewired (else None), "save": bool}.
    """

    BINDINGS = [Binding("ctrl+s", "save", "Save"), Binding("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    FlowScreen { align: center middle; }
    FlowScreen .modal-box { width: 110; height: 92%; }
    FlowScreen VerticalScroll { height: 1fr; }
    FlowScreen TextArea { height: 5; }
    FlowScreen TextArea.notes { height: 10; }
    FlowScreen .field-label { color: $text-muted; margin-top: 1; }
    FlowScreen #flow-bar { height: auto; margin-top: 1; }
    FlowScreen #flow-bar Button { margin-right: 1; }
    FlowScreen #flow-new-id { width: 18; }
    FlowScreen #flow-new-kind { width: 28; }
    FlowScreen #flow-error { color: $error; height: auto; }
    """
    GATES = ("human", "auto", "auto_answer", "none")

    def __init__(self, rows: list[dict], kinds: list[str], current: str | None = None, flow_name: str = ""):
        super().__init__()
        self.rows = [dict(r) for r in rows]
        self.initial = {r["step"]: dict(r) for r in rows}
        self.order0 = [r["step"] for r in rows]
        self.kinds = kinds
        self.current = current if current in self.initial else rows[0]["step"]
        self.flow_name = flow_name

    def row(self, step: str) -> dict:
        return next(r for r in self.rows if r["step"] == step)

    def _options(self) -> list[tuple[str, str]]:
        return [(f"{r['step']} — {r['title']}".strip(" —"), r["step"]) for r in self.rows]

    def compose(self) -> ComposeResult:
        from textual.containers import VerticalScroll
        from textual.widgets import Checkbox, Select, TextArea

        with Vertical(classes="modal-box"):
            yield Label(f"Flow {self.flow_name}", classes="modal-title")
            yield Select(self._options(), value=self.current, allow_blank=False, id="flow-step")
            with Horizontal(id="flow-bar"):
                yield Button("↑", id="up")
                yield Button("↓", id="down")
                yield Button("Remove", id="remove", variant="error")
                yield Input(placeholder="new step id", id="flow-new-id")
                yield Select([(k, k) for k in self.kinds], value=self.kinds[0], allow_blank=False, id="flow-new-kind")
                yield Button("Add", id="add", variant="primary")
            yield Static("", id="flow-error")
            with VerticalScroll():
                yield Label("Depends on (step ids, comma-separated)", classes="field-label")
                yield Input("", id="flow-deps")
                yield Label("Gate", classes="field-label")
                yield Select([(g, g) for g in self.GATES], value="none", allow_blank=False, id="flow-gate")
                yield Label("View — extra files shown in the step's Files tab (one glob per line)", classes="field-label")
                yield TextArea("", id="flow-view")
                yield Label("Pass — the step fails unless all hold: exists <glob> · contains <file> <regex> · run <cmd>",
                            classes="field-label")
                yield TextArea("", id="flow-pass")
                yield Label("Notes — extra instructions for the step's LLM tasks (markdown)", classes="field-label")
                yield TextArea("", id="flow-notes", soft_wrap=True, classes="notes")
            yield Checkbox("Only for this session (don't save settings to q3tui.yaml)", id="flow-session-only")
            with Horizontal(classes="modal-buttons"):
                yield Button("Save (ctrl+s)", id="save", variant="success")
                yield Button("Cancel (esc)", id="cancel")
            yield Static("Adding, removing, moving and rewiring steps writes the flow file; restart Q3TUI to use it.",
                         classes="modal-hint")

    def on_mount(self) -> None:
        self._load(self.current)

    def _load(self, step: str) -> None:
        from textual.widgets import Select, TextArea

        r = self.row(step)
        gate = self.query_one("#flow-gate", Select)
        gate.disabled = r["gate"] is None  # runs inside another step: no gate of its own
        gate.value = r["gate"] or "none"
        self.query_one("#flow-deps", Input).value = ", ".join(r["deps"] or [])
        self.query_one("#flow-view", TextArea).load_text("\n".join(r["view"]))
        self.query_one("#flow-pass", TextArea).load_text("\n".join(r["pass"]))
        self.query_one("#flow-notes", TextArea).load_text(r["notes"])

    def _store(self, step: str) -> None:
        from textual.widgets import Select, TextArea

        r = self.row(step)
        lines = lambda i: [x.strip() for x in self.query_one(i, TextArea).text.splitlines() if x.strip()]  # noqa: E731
        r["view"], r["pass"] = lines("#flow-view"), lines("#flow-pass")
        r["notes"] = self.query_one("#flow-notes", TextArea).text.strip()
        r["deps"] = [d.strip() for d in self.query_one("#flow-deps", Input).value.split(",") if d.strip()]
        if r["gate"] is not None:
            r["gate"] = self.query_one("#flow-gate", Select).value

    def _refresh_select(self) -> None:
        from textual.widgets import Select

        sel = self.query_one("#flow-step", Select)
        self._muted = True
        sel.set_options(self._options())
        sel.value = self.current
        self._load(self.current)
        self.call_after_refresh(setattr, self, "_muted", False)

    def on_select_changed(self, event) -> None:
        if event.select.id == "flow-step" and not getattr(self, "_muted", False) and event.value != self.current:
            self._store(self.current)  # keep the edits of the step we leave
            self.current = event.value
            self._load(self.current)

    def _error(self, text: str = "") -> None:
        self.query_one("#flow-error", Static).update(text)

    def _move(self, delta: int) -> None:
        self._store(self.current)
        i = next(n for n, r in enumerate(self.rows) if r["step"] == self.current)
        j = i + delta
        if 0 <= j < len(self.rows):
            self.rows[i], self.rows[j] = self.rows[j], self.rows[i]
            self._refresh_select()

    def _add(self) -> None:
        import re

        from textual.widgets import Select

        self._store(self.current)
        sid = self.query_one("#flow-new-id", Input).value.strip()
        kind = self.query_one("#flow-new-kind", Select).value
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", sid):
            return self._error("a step id: letters, digits, _ and -")
        if any(r["step"] == sid for r in self.rows):
            return self._error(f"'{sid}' already exists")
        self._error()
        i = next(n for n, r in enumerate(self.rows) if r["step"] == self.current)
        self.rows.insert(i + 1, {"step": sid, "title": "", "kind": kind, "deps": [self.current], "gate": "none", "view": [], "pass": [],
                                 "notes": "", "label": None, "raw_title": None, "options": {}})
        self.current = sid
        self.query_one("#flow-new-id", Input).value = ""
        self._refresh_select()

    def _remove(self) -> None:
        if len(self.rows) <= 1:
            return self._error("a flow needs at least one step")
        self._store(self.current)
        gone = self.current
        i = next(n for n, r in enumerate(self.rows) if r["step"] == gone)
        del self.rows[i]
        for r in self.rows:
            r["deps"] = [d for d in r["deps"] if d != gone]
        self._error()
        self.current = self.rows[min(i, len(self.rows) - 1)]["step"]
        self._refresh_select()

    def _result(self) -> dict:
        from textual.widgets import Checkbox

        self._store(self.current)
        changes: dict = {}
        for r in self.rows:
            was = self.initial.get(r["step"])
            if was is None:
                continue
            ch = {k: r[k] for k in ("gate", "view", "pass", "notes") if r[k] != was[k]}
            if ch:
                changes[r["step"]] = ch
        shape = lambda rows: [(r["step"], r["kind"], list(r["deps"] or [])) for r in rows]  # noqa: E731
        structure = [dict(r) for r in self.rows] if shape(self.rows) != shape(self.initial.values()) else None
        return {"changes": changes, "structure": structure, "save": not self.query_one("#flow-session-only", Checkbox).value}

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "save":
            self.dismiss(self._result())
        elif bid == "cancel":
            self.dismiss(None)
        elif bid == "up":
            self._move(-1)
        elif bid == "down":
            self._move(1)
        elif bid == "add":
            self._add()
        elif bid == "remove":
            self._remove()

    def action_save(self) -> None:
        self.dismiss(self._result())

    def action_cancel(self) -> None:
        self.dismiss(None)


class PromptsScreen(ModalScreen[None]):
    """Prompt editor: pick a step (all its stages) or a stage, read its built-in system prompt, edit your override.

    The override is appended to the built-in prompt (mode append) or replaces it (mode replace); empty removes it.
    `get(target) -> {builtin, mode, text, file}`, `put(target, text, mode)` and `targets` come from Ops.
    """

    BINDINGS = [Binding("ctrl+s", "save", "Save"), Binding("escape", "close", "Close")]
    DEFAULT_CSS = """
    PromptsScreen { align: center middle; }
    PromptsScreen .modal-box { width: 120; height: 92%; }
    PromptsScreen TextArea { height: 1fr; }
    PromptsScreen .field-label { color: $text-muted; margin-top: 1; }
    PromptsScreen #pr-status { color: $success; height: auto; }
    """

    def __init__(self, targets: list[str], get, put, current: str | None = None):
        super().__init__()
        self.targets, self.get, self.put = targets, get, put
        self.current = current if current in targets else targets[0]

    def compose(self) -> ComposeResult:
        from textual.widgets import Select, TextArea

        with Vertical(classes="modal-box"):
            yield Label("Prompts", classes="modal-title")
            yield Select([(t, t) for t in self.targets], value=self.current, allow_blank=False, id="pr-target")
            yield Label("Built-in system prompt(s) of its stages (recorded when the step runs; read-only)", classes="field-label")
            yield TextArea("", id="pr-builtin", read_only=True, soft_wrap=True)
            yield Label("Your override (markdown) — empty removes it", classes="field-label")
            yield Select([("append to the built-in prompt", "append"), ("replace the built-in prompt", "replace")], value="append",
                         allow_blank=False, id="pr-mode")
            yield TextArea("", id="pr-text", soft_wrap=True)
            yield Static("", id="pr-status")
            with Horizontal(classes="modal-buttons"):
                yield Button("Save (ctrl+s)", id="save", variant="success")
                yield Button("Close (esc)", id="close")
            yield Static("Files: <project>/prompts/<step or stage>.md · applies to the next LLM task", classes="modal-hint")

    def on_mount(self) -> None:
        self._load(self.current)

    def _load(self, target: str) -> None:
        from textual.widgets import Select, TextArea

        d = self.get(target)
        self.query_one("#pr-builtin", TextArea).load_text(d["builtin"] or "(not recorded yet: it appears here after the step ran once)")
        self.query_one("#pr-mode", Select).value = d["mode"]
        self.query_one("#pr-text", TextArea).load_text(d["text"])
        self.query_one("#pr-status", Static).update(f"override: {d['file']}" if d["file"] else "")

    def on_select_changed(self, event) -> None:
        if event.select.id == "pr-target" and event.value != self.current:
            self.current = event.value
            self._load(self.current)

    def action_save(self) -> None:
        from textual.widgets import Select, TextArea

        msg = self.put(self.current, self.query_one("#pr-text", TextArea).text, self.query_one("#pr-mode", Select).value)
        self.query_one("#pr-status", Static).update(str(msg))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self.action_save()
        else:
            self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)


class SkillScreen(ModalScreen[None]):
    """Skill editor: pick a step, edit its skill.md — front matter (kind, deps, gate, options, view, pass), notes for its LLM
    tasks, and the `<!-- NAME -->` sections that are its stage system prompts. `get(step) -> {text, file}`, `put(step, text)`."""

    BINDINGS = [Binding("ctrl+s", "save", "Save"), Binding("escape", "close", "Close")]
    DEFAULT_CSS = """
    SkillScreen { align: center middle; }
    SkillScreen .modal-box { width: 120; height: 92%; }
    SkillScreen TextArea { height: 1fr; }
    SkillScreen #sk-status { height: auto; color: $text-muted; }
    SkillScreen #sk-status.error { color: $error; }
    """

    def __init__(self, steps: list[str], get, put, current: str | None = None):
        super().__init__()
        self.steps, self.get, self.put = steps, get, put
        self.current = current if current in steps else steps[0]

    def compose(self) -> ComposeResult:
        from textual.widgets import Select, TextArea

        with Vertical(classes="modal-box"):
            yield Label("Skills", classes="modal-title")
            yield Select([(s, s) for s in self.steps], value=self.current, allow_blank=False, id="sk-step")
            yield TextArea("", id="sk-text", soft_wrap=True, show_line_numbers=True)
            yield Static("", id="sk-status")
            with Horizontal(classes="modal-buttons"):
                yield Button("Save (ctrl+s)", id="save", variant="success")
                yield Button("Close (esc)", id="close")
            yield Static("<flow folder>/<step>/md/skill.md · a built-in flow is copied to <project>/flows/ on the first save",
                         classes="modal-hint")

    def on_mount(self) -> None:
        self._load(self.current)

    def _load(self, step: str) -> None:
        from textual.widgets import TextArea

        d = self.get(step)
        self.query_one("#sk-text", TextArea).load_text(d["text"])
        self._status(d["file"])

    def _status(self, text: str, error: bool = False) -> None:
        w = self.query_one("#sk-status", Static)
        w.update(text)
        w.set_class(error, "error")

    def on_select_changed(self, event) -> None:
        if event.select.id == "sk-step" and event.value != self.current:
            self.current = event.value
            self._load(self.current)

    def action_save(self) -> None:
        from textual.widgets import TextArea

        try:
            self._status(str(self.put(self.current, self.query_one("#sk-text", TextArea).text)))
        except Exception as exc:  # noqa: BLE001
            self._status(str(exc), error=True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self.action_save()
        else:
            self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)


class DiagramScreen(ModalScreen[None]):
    """Diagrams full screen: the whole terminal, scrolled both ways (arrows / h j k l, shift+←/→ a page sideways,
    Home / End), n / p for the next / previous diagram of the same view."""

    BINDINGS = [
        Binding("escape,q", "close", "Close"),
        Binding("n", "move(1)", "Next"),
        Binding("p", "move(-1)", "Previous"),
        Binding("h", "pan(-8, 0)", "Left", show=False),
        Binding("l", "pan(8, 0)", "Right", show=False),
        Binding("k", "pan(0, -3)", "Up", show=False),
        Binding("j", "pan(0, 3)", "Down", show=False),
        Binding("shift+left", "page(-1)", "Page left", show=False),
        Binding("shift+right", "page(1)", "Page right", show=False),
    ]
    DEFAULT_CSS = """
    DiagramScreen { align: center middle; }
    DiagramScreen > #dg-box { width: 100%; height: 100%; border: round $accent; background: $surface; padding: 0 1; }
    DiagramScreen #dg-title { text-style: bold; height: auto; }
    DiagramScreen #dg-scroll { height: 1fr; overflow: auto auto; background: $boost; }
    DiagramScreen #dg-scroll > Static { width: auto; height: auto; padding: 0 1; }
    DiagramScreen #dg-hint { color: $text-muted; height: 1; }
    """

    def __init__(self, blocks: list[tuple[str, str, str]], index: int = 0):
        """`blocks`: (code, lang, caption) of every diagram of the view; `index`: the one to show first."""
        super().__init__()
        self.blocks, self.index = blocks, max(0, min(index, len(blocks) - 1))

    def compose(self) -> ComposeResult:
        with Vertical(id="dg-box"):
            yield Label("", id="dg-title")
            with ScrollableContainer(id="dg-scroll"):
                yield Static("", id="dg-art")
            yield Static("←/→/↑/↓ or h/l/k/j scroll · shift+←/→ page · Home/End · n/p next/previous diagram · Esc close",
                         id="dg-hint")

    def on_mount(self) -> None:
        self._show()
        self.query_one("#dg-scroll").focus()

    def _show(self) -> None:
        from q3tui.tui.views import MdCode

        code, lang, caption = self.blocks[self.index]
        title = f"Diagram {self.index + 1}/{len(self.blocks)}" + (f" — {caption}" if caption else f" ({lang})")
        self.query_one("#dg-title", Label).update(title)
        self.query_one("#dg-art", Static).update(MdCode.renderable(code, lang))
        self.query_one("#dg-scroll").scroll_home(animate=False)

    def action_move(self, step: int) -> None:
        if len(self.blocks) > 1:
            self.index = (self.index + step) % len(self.blocks)
            self._show()

    def action_pan(self, dx: int, dy: int) -> None:
        sc = self.query_one("#dg-scroll")
        sc.scroll_to(x=sc.scroll_x + dx, y=sc.scroll_y + dy, animate=False)

    def action_page(self, direction: int) -> None:
        sc = self.query_one("#dg-scroll")
        sc.scroll_to(x=sc.scroll_x + direction * max(8, sc.size.width - 8), animate=False)

    def action_close(self) -> None:
        self.dismiss(None)


class LanesScreen(ModalScreen[None]):
    """Lanes (docs/spec/lanes.md): the tags and each lane's result on them, read from the files every 2 s. t tags the design; r runs the lanes
    on the latest tag, m merges their fixes, c converges (lanes → merge → … until every lane passes on one tag): in the background."""

    BINDINGS = [Binding("escape,q", "close", "Close"), Binding("t", "tag", "Tag the design"), Binding("r", "run", "Run the lanes"),
                Binding("m", "merge", "Merge the fixes"), Binding("c", "converge", "Converge")]
    DEFAULT_CSS = """
    LanesScreen #lanes-box { width: 150; height: 85%; }
    LanesScreen DataTable { height: 1fr; }
    LanesScreen #lanes-detail { height: auto; max-height: 8; padding: 0 1; }
    """
    STYLE = {"passed": "green", "fixed": "cyan", "failed": "red", "needs_you": "yellow"}

    def __init__(self, ops, start):
        super().__init__()
        self.ops, self.start = ops, start  # start(args) → message: a `q3tui …` command in the background

    def compose(self) -> ComposeResult:
        from textual.widgets import DataTable

        with Vertical(id="lanes-box", classes="modal-box"):
            yield Label("Lanes", classes="modal-title")
            yield DataTable(id="lanes-table", zebra_stripes=True, cursor_type="row")
            yield Static("", id="lanes-detail")
            yield Static("t: tag the design · r: run the lanes · m: merge their fixes · c: converge · Esc: close", classes="modal-hint")

    def on_mount(self) -> None:
        from textual.widgets import DataTable

        self.query_one(DataTable).add_columns("Tag", "Lane", "Status", "Cost", "Detail")
        self._fill()
        self.set_interval(2.0, self._fill)

    def _rows(self) -> list[tuple]:
        from rich.text import Text

        from q3tui.core import lanes

        project = self.ops.project
        rows = []
        for t in reversed(lanes.list_tags(project)):
            name = t["name"] + (" ✔" if (lanes.tags_dir(project) / t["name"] / "converged").is_file() else "")
            results = lanes.tag_results(project, t["name"])
            if not results:
                rows.append((t["name"], name, "–", Text("no lane run yet", "grey50"), "", t["at"].replace("T", " ")))
            for lane, r in results.items():
                st = str(r.get("status", "?"))
                rows.append((f"{t['name']}/{lane}", name, lane, Text(st, self.STYLE.get(st, "")), f"${r.get('cost_usd', 0):.2f}",
                             " ".join(str(r.get("detail") or "").split())[:110]))
        return rows

    def _fill(self) -> None:
        from textual.widgets import DataTable

        from q3tui.tui.views import fill_table

        rows = self._rows()
        fill_table(self.query_one(DataTable), rows)
        if not rows:
            self.query_one("#lanes-detail", Static).update("no tag yet: t freezes the design (the design flows done and approved)")

    def on_data_table_row_highlighted(self, event) -> None:
        from q3tui.core import lanes

        key = event.row_key.value if event.row_key else ""
        tag, _, lane = str(key).partition("/")
        r = lanes.tag_results(self.ops.project, tag).get(lane, {}) if lane else {}
        text = (f"{lane} on {tag}: {r.get('status')} — {r.get('detail') or ''}\nfolder: {r.get('folder', '')}"
                + (f"\nrequests to the design: {r['requests']}" if r.get("requests") else "")
                + (f" · own fixes: {', '.join(r['own_fixes'])}" if r.get("own_fixes") else "")) if r else ""
        self.query_one("#lanes-detail", Static).update(text)

    def action_close(self) -> None:
        self.dismiss(None)

    def action_tag(self) -> None:
        try:
            self.app.notify(self.ops.tag())
        except Exception as exc:  # noqa: BLE001 - said, nothing is tagged
            self.app.notify(str(exc), severity="error", timeout=12)
        self._fill()

    def action_run(self) -> None:
        self.app.notify(self.start(["lanes", "run"]))

    def _confirmed(self, question: str, detail: str, args: list[str]) -> None:
        self.app.push_screen(ConfirmScreen(question, detail), lambda ok: self.app.notify(self.start(args)) if ok else None)

    def action_merge(self) -> None:
        try:
            preview = self.ops.lanes_merge_preview()
        except Exception as exc:  # noqa: BLE001
            self.app.notify(str(exc), severity="error")
            return
        self._confirmed("Merge the lanes' fixes into the project folder?", preview + "\nThen the design flows run and the next tag is made.",
                        ["lanes", "merge", "--yes"])

    def action_converge(self) -> None:
        self._confirmed("Converge?", "Lanes on the latest tag → merge their fixes → the next tag → lanes again, until every lane passes on one "
                                     "tag (bounded by lanes.max_rounds / lanes.max_cost_usd). Runs in the background.", ["lanes", "converge", "--yes"])
