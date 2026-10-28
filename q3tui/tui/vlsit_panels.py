"""Panels of the spec flow's step kinds (flows/spec/; docs/spec/spec-flow.md), built on the JSON artifacts.

Every panel is tolerant: the artifacts (flows/spec/lib/artifacts.py) may not exist yet, or have fewer keys than the
full schema; the review API (flows/spec/parse/review.py, config/review.py) is reached through `Ops`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll, Vertical
from textual.widgets import DataTable, Static, TabbedContent, TabPane

from q3tui.core.icons import icon
from q3tui.tui.panels import DefaultPanel, register_panel
from q3tui.tui.views import FileViewer, IdTable, Markdown, QuestionsTable, ResultBanner, fill_table, set_markdown, short

if TYPE_CHECKING:
    from q3tui.pipeline.base import StepDef
    from q3tui.tui.app import Q3TUIApp

def OK() -> str:  # (the glyphs follow the icon set — `tui.icons`: unicode / nerd / ascii — at the moment they are drawn)
    return icon("pass")


def BAD() -> str:
    return icon("fail")


def NA() -> str:
    return icon("na")

_JSON_CACHE: dict[str, tuple[tuple[int, int], object]] = {}


def read_artifact(path: Path, default=None):
    """A JSON file, cached by (mtime, size): panels refresh on every event, the file rarely changes."""
    try:
        st = path.stat()
    except OSError:
        return default
    sig = (st.st_mtime_ns, st.st_size)
    hit = _JSON_CACHE.get(str(path))
    if hit and hit[0] == sig:
        return hit[1]
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return default
    _JSON_CACHE[str(path)] = (sig, data)
    return data


def artifact(app: "Q3TUIApp", name: str, default=None):
    return read_artifact(app.project.schemas_dir / name, default)


def _tick(v) -> Text:
    if v is True:
        return Text(OK(), "green")
    if v is False:
        return Text(BAD(), "red")
    return Text(NA(), "grey50")


def _score(v) -> Text:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return Text(f"{v:.0%}", "green" if v >= 0.85 else "red")
    return Text(str(v if v is not None else "pending"), "grey50")


# -- a table with a detail pane ---------------------------------------------------------------------------------


@dataclass
class TabSpec:
    id: str
    title: str
    columns: tuple[str, ...]
    rows: Callable[["Q3TUIApp"], list[tuple]]  # [(key, *cells)]
    detail: Callable[["Q3TUIApp", str], str] | None = None  # (app, row key) -> Markdown
    hint: str = ""
    # how many rows the hint's action (confirm / sign off / …) would currently apply to: 0 — nothing to do right
    # now, the hint stays muted; >0 — the hint highlights so it is not just more grey text to skim past
    urgent: Callable[["Q3TUIApp"], int] | None = None
    widget: type | None = None  # a DataTab subclass (key bindings)
    empty: str = "nothing here yet"
    summary: Callable[["Q3TUIApp"], str] | None = None  # a line in front of the hint that changes with the rows (counts: "60 tests: 55 PASS · 2 FAIL")


class DataTab(Vertical):
    """Rows of one JSON artifact (left) and the highlighted row's detail (right)."""

    DEFAULT_CSS = """
    DataTab > Horizontal { height: 1fr; }
    DataTab IdTable { width: 3fr; }
    DataTab VerticalScroll { width: 2fr; padding: 0 1; }
    DataTab .dt-hint { height: auto; }
    """

    def __init__(self, tab: TabSpec, **kw):
        super().__init__(classes="refreshable", **kw)
        self.tab = tab
        self._shown: tuple | None = None

    def compose(self) -> ComposeResult:
        yield Static(self.tab.hint, classes="hint dt-hint", id="dt-hint")
        with Horizontal():
            yield IdTable(cursor_type="row", zebra_stripes=True)
            with VerticalScroll():
                yield Markdown()

    def on_mount(self) -> None:
        self.query_one(DataTable).add_columns(*self.tab.columns)
        self._refresh_hint()

    def _refresh_hint(self) -> None:
        if (self.tab.urgent is None and self.tab.summary is None) or not self.tab.hint:
            return
        try:
            n = self.tab.urgent(self.capp) if self.tab.urgent else 0
            lead = self.tab.summary(self.capp) if self.tab.summary else ""
        except Exception:  # noqa: BLE001 - the hint never breaks the refresh
            n, lead = 0, ""
        hint = self.query_one("#dt-hint", Static)
        if lead:
            hint.update(Text(f"{'⚠ ' if n else ''}{lead} — {self.tab.hint}"))
        elif n:
            hint.update(f"⚠ {n} waiting — {self.tab.hint}")
        else:
            hint.update(self.tab.hint)
        hint.set_classes("hint dt-hint urgent" if n else "hint dt-hint")

    @property
    def capp(self) -> "Q3TUIApp":
        return self.app  # type: ignore[return-value]

    def current_key(self) -> str | None:
        table = self.query_one(DataTable)
        if not table.row_count:
            return None
        return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value

    def refresh_view(self) -> None:
        table = self.query_one(DataTable)
        rows = self.tab.rows(self.capp)
        fill_table(table, rows)
        self._detail(force=True)
        if not rows:
            set_markdown(self.query_one(Markdown), f"_{self.tab.empty}_")
        self._refresh_hint()

    def _detail(self, force: bool = False) -> None:
        if self.tab.detail is None:
            return
        key = self.current_key()
        if key is None:
            return
        try:
            text = self.tab.detail(self.capp, key)
        except Exception as exc:  # noqa: BLE001 - a detail pane never breaks the refresh
            text = f"_cannot show {key}: {exc}_"
        set_markdown(self.query_one(Markdown), text)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        event.stop()
        self._detail()

    def notify_error(self, exc: Exception) -> None:
        self.app.notify(str(exc), severity="error")


class VlsitPanel(DefaultPanel):
    """Default panel + this kind's tables first."""

    tabs: tuple[TabSpec, ...] = ()

    def __init__(self, step: "StepDef", tabs: tuple[TabSpec, ...] = (), **kw):
        super().__init__(step, **kw)
        self.tabs = tabs
        self.main_tab = tabs[0].id if tabs else "files"

    def compose(self) -> ComposeResult:
        yield ResultBanner()
        with TabbedContent():
            for t in self.tabs:
                with TabPane(t.title, id=f"{self._pid}-{t.id}"):
                    yield (t.widget or DataTab)(t)
            with TabPane("Files", id=f"{self._pid}-files"):
                yield FileViewer(self.step_name)
            with TabPane("Questions", id=f"{self._pid}-questions"):
                yield QuestionsTable(self.step_name)


# -- parse -------------------------------------------------------------------------------------------------------


def _reqs(app) -> list[dict]:
    data = artifact(app, "structured_spec.json", {}) or {}
    return [r for r in data.get("requirements", []) if isinstance(r, dict)]


def _parse_urgent(app) -> int:
    try:
        return len(app.engine.by_name["parse"].pending_reviews(app.engine))
    except Exception:  # noqa: BLE001
        return 0


def _parse_reviews(app) -> dict[str, dict]:
    try:
        return app.ops.requirement_reviews()
    except Exception:  # noqa: BLE001
        return {}


def _parse_rows(app) -> list[tuple]:
    revs = _parse_reviews(app)
    rows = []
    for r in _reqs(app):
        amb = r.get("ambiguity_score")
        rid = r.get("req_id", "?")
        confirmed = r.get("needs_review") and revs.get(rid, {}).get("status") == "confirmed"
        if r.get("needs_human_decision"):
            state, style = "decide", "red"
        elif confirmed:
            state, style = "confirmed", "cyan"
        elif r.get("needs_review"):
            state, style = "review", "yellow"
        else:
            state, style = OK(), "green"
        rows.append((rid, rid, r.get("category", ""),
                     Text(f"{amb:.2f}" if isinstance(amb, (int, float)) else "", "red" if r.get("needs_review") or r.get("needs_human_decision") else ""),
                     Text(state, style), short(r.get("text", ""), 80)))
    return rows


def _parse_detail(app, rid: str) -> str:
    r = next((x for x in _reqs(app) if x.get("req_id") == rid), None)
    if r is None:
        return ""
    lines = [f"### {rid} — {r.get('category', '')}", "", r.get("text", ""), ""]
    if r.get("needs_review"):
        rev = _parse_reviews(app).get(rid, {})
        lines.append(f"- **review:** {'confirmed' if rev.get('status') == 'confirmed' else 'pending'}")
    for label, key in (("Source", "source_section"), ("Feature", "feature_id"), ("SVA hint", "sva_hint"), ("Ambiguity", "ambiguity_score")):
        if r.get(key) not in (None, ""):
            lines.append(f"- **{label}:** {r[key]}")
    for label, key in (("Parameters", "parameters_affected"), ("RTL modules", "rtl_modules")):
        if r.get(key):
            lines.append(f"- **{label}:** {', '.join(map(str, r[key]))}")
    return "\n".join(lines)


def _param_rows(app) -> list[tuple]:
    data = artifact(app, "structured_spec.json", {}) or {}
    return [(p.get("name", "?"), p.get("name", "?"), p.get("type", ""), p.get("default", ""), short(p.get("valid_range", ""), 40),
             _tick(p.get("locked")), ", ".join(p.get("req_ids") or [])) for p in data.get("parameters", []) if isinstance(p, dict)]


def _parse_header(app) -> str:
    data = artifact(app, "structured_spec.json", {}) or {}
    meta = data.get("metadata", {})
    reqs = _reqs(app)
    need = sum(bool(r.get("needs_review")) for r in reqs)
    decide = sum(bool(r.get("needs_human_decision")) for r in reqs)
    return (f"{meta.get('ip_name', '')}: {len(reqs)} requirements · {need} need review · {decide} need your decision · "
            f"gate 1 {meta.get('gate_status', 'pending')}")


class ParseReqTab(DataTab):
    """Requirements flagged for review: c confirm (read it, it's fine) · u back to pending · C confirm all.
    A requirement that needs your decision (red "decide") is a blocking question instead — answer it (o)."""

    BINDINGS = [Binding("c", "review(True)", "Confirm"), Binding("u", "review(False)", "Pending"),
                Binding("C", "confirm_all", "Confirm all")]

    def action_confirm_all(self) -> None:
        try:
            self.app.log_info(self.capp.ops.confirm_requirements())  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            self.notify_error(exc)
        self.capp.refresh_all()

    def action_review(self, confirmed: bool) -> None:
        rid = self.current_key()
        if rid is None:
            return
        try:
            self.app.log_info(self.capp.ops.review_parse_requirement(rid, confirmed))  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 - friendly message, never a traceback
            self.notify_error(exc)
        self.capp.refresh_all()


# -- config ------------------------------------------------------------------------------------------------------


def _cfg_reviews(app) -> dict[str, dict]:
    try:
        return app.ops.parameter_reviews()
    except Exception:  # noqa: BLE001
        return {}


def _config_urgent(app) -> int:
    try:
        return len(app.engine.by_name["config"].pending_reviews(app.engine))
    except Exception:  # noqa: BLE001
        return 0


def _cfg_param_rows(app) -> list[tuple]:
    data = artifact(app, "final_config.json", {}) or {}
    revs = _cfg_reviews(app)
    rows = []
    for name, p in (data.get("parameters") or {}).items():
        if not isinstance(p, dict):
            p = {"value": p}
        changed = bool(p.get("changed_from_default"))
        confirmed = changed and revs.get(name, {}).get("status") == "confirmed"
        state = Text("confirmed", "cyan") if confirmed else (Text("review", "yellow") if changed else Text(OK(), "green"))
        rows.append((name, name, str(p.get("value", "")), str(p.get("default", "")),
                     Text("changed", "yellow") if changed else Text("default", "grey50"),
                     Text("locked", "cyan") if p.get("locked") else "", state))
    return rows


def _constraint_rows(app) -> list[tuple]:
    data = artifact(app, "final_config.json", {}) or {}
    return [(cid, cid, _tick(c.get("pass")) if isinstance(c, dict) else _tick(None), short((c or {}).get("rule", ""), 50),
             short((c or {}).get("detail", ""), 60)) for cid, c in (data.get("constraints") or {}).items()]


def _cfg_detail(app, name: str) -> str:
    data = artifact(app, "final_config.json", {}) or {}
    p = (data.get("parameters") or {}).get(name)
    if not isinstance(p, dict):
        return ""
    lines = [f"### {name}", "", f"- value `{p.get('value')}` (default `{p.get('default')}`, type `{p.get('type', '')}`)"]
    if p.get("changed_from_default"):
        rev = _cfg_reviews(app).get(name, {})
        lines.append(f"- **review:** {'confirmed' if rev.get('status') == 'confirmed' else 'pending'}")
    if p.get("req_ids_affected"):
        lines.append(f"- REQs: {', '.join(p['req_ids_affected'])}")
    if p.get("feature_ids_affected"):
        lines.append(f"- features: {', '.join(p['feature_ids_affected'])}")
    return "\n".join(lines)


class ConfigParamTab(DataTab):
    """Parameters changed from default: c confirm (read it, it's fine) · u back to pending · C confirm all.
    A parameter kept at its default needs no review."""

    BINDINGS = [Binding("c", "review(True)", "Confirm"), Binding("u", "review(False)", "Pending"),
                Binding("C", "confirm_all", "Confirm all")]

    def action_confirm_all(self) -> None:
        try:
            self.app.log_info(self.capp.ops.confirm_parameters())  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            self.notify_error(exc)
        self.capp.refresh_all()

    def action_review(self, confirmed: bool) -> None:
        name = self.current_key()
        if name is None:
            return
        try:
            self.app.log_info(self.capp.ops.review_parameter(name, confirmed))  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 - friendly message, never a traceback
            self.notify_error(exc)
        self.capp.refresh_all()


def _tabs_for(kind: str) -> tuple[TabSpec, ...]:
    if kind == "vlsit_parse":
        return (TabSpec("reqs", "Requirements", ("REQ", "Category", "Ambig.", "State", "Text"), _parse_rows, _parse_detail,
                        "c confirm · u pending · C confirm all — \"decide\" (red) is a blocking question instead, answer it (o); "
                        "Gate 1 waits for every \"review\" (yellow) to be confirmed",
                        urgent=_parse_urgent, widget=ParseReqTab, empty="no structured_spec.json yet — run (r)"),
                TabSpec("params", "Parameters", ("Name", "Type", "Default", "Range", "Locked", "REQs"), _param_rows))
    if kind == "vlsit_config":
        return (TabSpec("params", "Parameters", ("Name", "Value", "Default", "Change", "Locked", "Confirmed"), _cfg_param_rows, _cfg_detail,
                        "c confirm · u pending · C confirm all — a parameter at its default needs no review; "
                        "Gate 2 waits for every changed one to be confirmed",
                        urgent=_config_urgent, widget=ConfigParamTab, empty="no final_config.json yet — run (r)"),
                TabSpec("constraints", "Constraints", ("ID", "Pass", "Rule", "Detail"), _constraint_rows,
                        hint="all constraints must pass before Gate 2 signs itself"))
    return ()


def _builder(kind: str):
    def build(step: "StepDef"):
        return VlsitPanel(step, _tabs_for(kind))

    return build


for _kind in ("vlsit_parse", "vlsit_config"):
    register_panel(_kind, _builder(_kind))

HEADERS = {"vlsit_parse": _parse_header}  # one-line summaries for the gate dialog
