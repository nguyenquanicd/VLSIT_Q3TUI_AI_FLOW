"""The picker: a Claude-Code-style choice dialog (docs/spec/tui.md "The picker").

A numbered list of options with descriptions (↑/↓, Enter, number keys, Space toggles in multi-select), an always
present "Other…" that opens the multi-line text prompt, an optional preview pane, a "(recommended)" marker. Used to
answer questions, for the review gate, reset scope, gate modes, confirmations and the assistant's `ask_user`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Label, OptionList, Static
from textual.widgets.option_list import Option

from q3tui.tui.i18n import t as t_

OTHER = "__other__"
_DEFAULT_OTHER = object()  # "not given": resolved via t() at construction time, so a later language switch applies;
                           # other=None (explicit) still means "no Other option" — kept distinct from this sentinel


@dataclass
class Choice:
    value: str
    label: str
    description: str = ""
    recommended: bool = False
    preview: str | None = None  # shown in the side pane while the option is highlighted
    key: str | None = None  # a letter that picks it at once (a / q / e …)
    style: str = ""  # Rich style of the label (e.g. "green")


@dataclass
class ChoiceResult:
    values: list[str] = field(default_factory=list)  # the picked option value(s); empty when `text` was typed
    text: str | None = None  # what was typed under "Other…"

    @property
    def value(self) -> str | None:
        return self.values[0] if self.values else None

    @property
    def answer(self) -> str:
        """The answer as one text: what was typed, else the picked values (joined for multi-select)."""
        return self.text if self.text is not None else ", ".join(self.values)


def choices_from(options, recommended: str | None = None) -> list[Choice]:
    """Choices from a question's `options` ([{label, description}] or plain strings); `recommended` marks the one whose
    label equals that text."""
    out: list[Choice] = []
    for o in options or []:
        if isinstance(o, str):
            label, desc = o, ""
        else:
            label, desc = str(o.get("label") or o.get("value") or ""), str(o.get("description") or "")
        if label.strip():
            out.append(Choice(label, label, desc, recommended=bool(recommended) and label.strip() == recommended.strip()))
    return out


class ChoiceScreen(ModalScreen):
    """Pick one option (or several), or type something else.

    Dismisses with a `ChoiceResult` (None on Esc). Subclasses (gate, reset, confirm) override `finish` to return
    their own value type.
    """

    BINDINGS = [
        Binding("escape", "cancel", t_("picker.cancel")),
        Binding("space", "toggle", t_("picker.toggle"), show=False),
        Binding("ctrl+s", "confirm", t_("picker.confirm"), show=False),
        *[Binding(str(i), f"pick({i})", show=False) for i in range(1, 10)],
    ]
    DEFAULT_CSS = """
    ChoiceScreen { align: center middle; }
    ChoiceScreen .modal-box { width: 100; }
    ChoiceScreen #choice-body { height: auto; max-height: 12; margin: 0 0 1 0; }
    ChoiceScreen #choice-body > Static { color: $text-muted; }
    ChoiceScreen #choice-main { height: auto; max-height: 24; }
    ChoiceScreen OptionList { height: auto; max-height: 20; border: none; padding: 0; background: transparent; }
    ChoiceScreen #choice-preview { width: 1fr; height: auto; max-height: 20; margin-left: 2; padding: 0 1; border-left: solid $panel-lighten-2; }
    ChoiceScreen #choice-preview > Static { color: $text; }
    """

    def __init__(self, title: str, choices: list[Choice], body: str = "", multi: bool = False, other: str | None = _DEFAULT_OTHER,
                 other_title: str | None = None, other_value: str = "", markup_body: bool = False):
        super().__init__()
        self.title_text, self.choices, self.body, self.multi = title, list(choices), body, multi
        self.other = t_("picker.other") if other is _DEFAULT_OTHER else other
        self.other_value = other_value
        self.other_title = other_title or title
        self.markup_body = markup_body
        self.checked: set[int] = set()
        self.has_preview = any(c.preview for c in self.choices)

    # -- layout -------------------------------------------------------------------------------

    def _entries(self) -> list[Choice]:
        """The list as shown: the choices, then Other… when allowed."""
        rows = list(self.choices)
        if self.other:
            rows.append(Choice(OTHER, self.other, t_("picker.type_own") if not self.multi else t_("picker.add_typed")))
        return rows

    def _prompt(self, i: int, c: Choice) -> Text:
        t = Text()
        mark = ""
        if self.multi and c.value != OTHER:
            mark = "[x] " if i in self.checked else "[ ] "
        t.append(f"{i + 1}. " if i < 9 else "   ")
        t.append(mark)
        t.append(c.label, c.style or ("bold" if c.recommended else ""))
        if c.recommended:
            t.append(t_("picker.recommended"), "green")
        if c.key:
            t.append(f"  ({c.key})", "grey50")
        if c.description:
            t.append("\n     " + c.description.replace("\n", "\n     "), "grey62")
        return t

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box"):
            yield Label(self.title_text, classes="modal-title")
            if self.body:
                with VerticalScroll(id="choice-body"):
                    yield Static(self.body, markup=self.markup_body)
            with Horizontal(id="choice-main"):
                yield OptionList(*[Option(self._prompt(i, c), id=f"opt-{i}") for i, c in enumerate(self._entries())], id="choice-list")
                if self.has_preview:
                    with VerticalScroll(id="choice-preview"):
                        yield Static("", id="choice-preview-text", markup=False)
            yield Static(self._hint(), classes="modal-hint", id="choice-hint")

    def _hint(self) -> str:
        if self.multi:
            return t_("picker.hint_multi")
        return t_("picker.hint_single") + (t_("picker.hint_other_suffix") if self.other else "")

    def on_mount(self) -> None:
        lst = self.query_one(OptionList)
        start = next((i for i, c in enumerate(self._entries()) if c.recommended), 0)
        lst.highlighted = start
        lst.focus()
        self._preview(start)

    # -- behaviour ----------------------------------------------------------------------------

    def _preview(self, index: int | None) -> None:
        if not self.has_preview or index is None:
            return
        rows = self._entries()
        text = rows[index].preview if 0 <= index < len(rows) else None
        self.query_one("#choice-preview-text", Static).update(text or "")

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        self._preview(event.option_index)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self._pick(event.option_index, enter=True)

    def _pick(self, index: int, enter: bool = False) -> None:
        rows = self._entries()
        if not 0 <= index < len(rows):
            return
        row = rows[index]
        if row.value == OTHER:
            self._ask_text()
        elif self.multi and enter and self.checked:
            self.checked.add(index)  # (Enter on a highlighted row adds it to the ticked ones)
            self.action_confirm()
        elif self.multi and enter:
            self._finish([row.value], None)
        else:
            self._finish([row.value], None)

    def action_pick(self, number: int) -> None:
        index = number - 1
        rows = self._entries()
        if not 0 <= index < len(rows):
            return
        lst = self.query_one(OptionList)
        lst.highlighted = index
        if self.multi:
            if rows[index].value == OTHER:
                self._ask_text()
            else:
                self.action_toggle()
        else:
            self._pick(index)

    def action_toggle(self) -> None:
        if not self.multi:
            return
        lst = self.query_one(OptionList)
        i = lst.highlighted
        rows = self._entries()
        if i is None or rows[i].value == OTHER:
            return
        self.checked.symmetric_difference_update({i})
        lst.replace_option_prompt_at_index(i, self._prompt(i, rows[i]))

    def action_confirm(self) -> None:
        rows = self._entries()
        self._finish([rows[i].value for i in sorted(self.checked)], None)

    def action_cancel(self) -> None:
        self.finish(None)

    def on_key(self, event) -> None:
        """A choice's own letter (a / q / e …) picks it at once."""
        if len(event.key) == 1:
            for i, c in enumerate(self._entries()):
                if c.key and c.key == event.key:
                    event.stop()
                    event.prevent_default()
                    self.query_one(OptionList).highlighted = i
                    self._pick(i)
                    return

    def _ask_text(self) -> None:
        from q3tui.tui.screens import TextPromptScreen

        def typed(text: str | None) -> None:
            if text:
                self._finish([], text)

        self.app.push_screen(TextPromptScreen(self.other_title, self.body if self.body else "", value=self.other_value), typed)

    def _finish(self, values: list[str], text: str | None) -> None:
        if self.multi and text is not None and self.checked:
            rows = self._entries()
            values = [rows[i].value for i in sorted(self.checked)]
        self.finish(ChoiceResult(values, text))

    def finish(self, result: ChoiceResult | None):
        """Leave the dialog (subclasses return their own type)."""
        self.dismiss(result)
