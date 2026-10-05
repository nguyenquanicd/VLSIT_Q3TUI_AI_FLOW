"""Panels of the VLSIT flow's step kinds (flows/vlsit/; docs/spec/vlsit-flow.md), built on the JSON artifacts.

Every panel is tolerant: the artifacts (steps/vlsit/artifacts.py) may not exist yet, or have fewer keys than the
full schema; the review API (steps/vlsit/review.py) is reached through `Ops` and a missing module is only a message.
"""

from __future__ import annotations

import json
import re
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
from q3tui.pipeline.engine import EngineError
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
        if self.tab.urgent is None or not self.tab.hint:
            return
        try:
            n = self.tab.urgent(self.capp)
        except Exception:  # noqa: BLE001 - the hint never breaks the refresh
            n = 0
        hint = self.query_one("#dt-hint", Static)
        if n:
            hint.update(f"⚠ {n} waiting — {self.tab.hint}")
            hint.set_classes("hint dt-hint urgent")
        else:
            hint.update(self.tab.hint)
            hint.set_classes("hint dt-hint")

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


# -- rtl ---------------------------------------------------------------------------------------------------------

_REQ_TAG = re.compile(r"REQ-\d{3,}")


def _rtl_files(app) -> list[Path]:
    d = app.project.src_dir / "rtl"
    return sorted(p for p in d.rglob("*") if p.suffix in (".sv", ".v", ".svh")) if d.is_dir() else []


def _rtl_rows(app) -> list[tuple]:
    rows = []
    synth = artifact(app, "synth_report.json", {}) or {}
    per = synth.get("modules") if isinstance(synth.get("modules"), dict) else {}
    for p in _rtl_files(app):
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        mod = per.get(p.stem, {}) if isinstance(per, dict) else {}
        cells = mod.get("cells", mod.get("cell_count", ""))
        rows.append((str(p), p.name, len(text.splitlines()), len(set(_REQ_TAG.findall(text))), str(cells), str(mod.get("area_um2", mod.get("area", "")))))
    return rows


def _rtl_detail(app, path: str) -> str:
    p = Path(path)
    try:
        head = p.read_text(errors="replace")
    except OSError:
        return ""
    tags = sorted(set(_REQ_TAG.findall(head)))
    return f"### {p.name}\n\nREQ tags: {', '.join(tags) or '—'}\n\n```systemverilog\n{head[:6000]}\n```"


def _synth_rows(app) -> list[tuple]:
    synth = artifact(app, "synth_report.json", {}) or {}
    rows = []

    def walk(prefix: str, node):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(f"{prefix}{k}.", v) if isinstance(v, dict) else rows.append((prefix + k, prefix + k, short(json.dumps(v) if isinstance(v, (list, dict)) else str(v), 90)))

    walk("", synth)
    return rows


# -- tb ----------------------------------------------------------------------------------------------------------


def _plan(app) -> dict:
    return artifact(app, "selected_testplan.json", {}) or {}


def _tc_rows(app) -> list[tuple]:
    return [(tc.get("tc_id", "?"), tc.get("tc_id", "?"), short(tc.get("title", ""), 50), ", ".join(tc.get("req_ids", [])),
             _tick(tc.get("selected", True))) for tc in _plan(app).get("test_cases", []) if isinstance(tc, dict)]


def _uncovered_rows(app) -> list[tuple]:
    covered = {r for tc in _plan(app).get("test_cases", []) if isinstance(tc, dict) and tc.get("selected", True) for r in tc.get("req_ids", [])}
    return [(r["req_id"], r["req_id"], r.get("category", ""), short(r.get("text", ""), 90)) for r in _reqs(app)
            if r.get("req_id") and r["req_id"] not in covered]


def _tb_urgent(app) -> int:
    try:
        return len(_uncovered_rows(app))
    except Exception:  # noqa: BLE001
        return 0


# -- sva ---------------------------------------------------------------------------------------------------------

_STATUS_STYLE = {"confirmed": "green", "rejected": "red", "pending": "yellow"}


def _properties(app) -> tuple[list[dict], str]:
    """(properties, note): the review module's list, else the labels recorded in rtm.json."""
    try:
        return [dict(p) for p in app.ops.properties()], ""
    except EngineError as exc:
        note = str(exc)
    except Exception as exc:  # noqa: BLE001
        note = f"properties unavailable: {exc}"
    rtm = artifact(app, "rtm.json", {}) or {}
    rows = [{"label": a, "req_ids": [r.get("req_id")], "status": "confirmed", "module": ""} for r in rtm.get("requirements", []) if isinstance(r, dict)
            for a in r.get("sva_assertions", []) or []]
    return rows, note


def _sva_urgent(app) -> int:
    try:
        return len(app.engine.by_name["sva"].pending_reviews(app.engine))
    except Exception:  # noqa: BLE001
        return 0


def _sva_rows(app) -> list[tuple]:
    props, _ = _properties(app)
    out = []
    for p in props:
        st = str(p.get("status", "pending"))
        vac = p.get("vacuous")
        key = str(p.get("id") or p.get("label"))  # (module:label when two modules share a label)
        out.append((key, key, p.get("module", ""), ", ".join(p.get("req_ids") or p.get("reqs") or []),
                    p.get("kind", "assert"), Text("vacuous", "red") if vac else (Text(OK(), "green") if vac is False else Text(NA(), "grey50")),
                    Text(st, _STATUS_STYLE.get(st, ""))))
    return out


def _sva_detail(app, label: str) -> str:
    props, note = _properties(app)
    p = next((x for x in props if str(x.get("id") or x.get("label")) == label), None)
    if p is None:
        return note
    lines = [f"### {label}", "", f"**NL:** {p.get('nl') or p.get('description') or '—'}", "",
             f"- module `{p.get('module', '')}` · REQs {', '.join(p.get('req_ids') or []) or '—'}",
             f"- review: **{p.get('status', 'pending')}**" + (f" — {p['note']}" if p.get("note") else "")]
    if p.get("code") or p.get("text"):
        lines += ["", "```systemverilog", str(p.get("code") or p.get("text")), "```"]
    return "\n".join(lines)


class SvaTab(DataTab):
    """Assertions with their NL text: c confirm · x reject (with a reason) · u back to pending."""

    BINDINGS = [Binding("c", "review('confirmed')", "Confirm"), Binding("x", "review('rejected')", "Reject"),
                Binding("u", "review('pending')", "Pending"), Binding("C", "confirm_all", "Confirm all")]

    def action_confirm_all(self) -> None:
        try:
            self.app.log_info(self.capp.ops.confirm_properties())  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            self.notify_error(exc)
        self.capp.refresh_all()

    def action_review(self, status: str) -> None:
        label = self.current_key()
        if label is None:
            return

        def apply(note: str = "") -> None:
            try:
                self.app.log_info(self.capp.ops.review_property(label, status, note))  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001 - friendly message, never a traceback
                self.notify_error(exc)
            self.capp.refresh_all()

        if status == "rejected":
            self.app.push_screen(self.capp.prompt_screen(f"Reject {label}", "Why? The SVA step rewrites this assertion with your reason."),
                                 lambda text: apply(text) if text else None)
        else:
            apply()


# -- verify ------------------------------------------------------------------------------------------------------


def _rtm_rows_data(app) -> list[dict]:
    try:
        return [dict(r) for r in app.ops.rtm()]
    except Exception:  # noqa: BLE001 - the review module may not be there: read the artifact
        rtm = artifact(app, "rtm.json", {}) or {}
        return [r for r in rtm.get("requirements", []) if isinstance(r, dict)]


def _rtm_urgent(app) -> int:
    try:
        return sum(r.get("status") == "pending" for r in _rtm_rows_data(app))  # ready to sign (conditions 1-5 hold), not yet signed
    except Exception:  # noqa: BLE001
        return 0


def _rtm_rows(app) -> list[tuple]:
    out = []
    for r in _rtm_rows_data(app):
        rid = r.get("req_id") or r.get("req") or "?"
        out.append((rid, rid, r.get("category", ""), _tick(r.get("rtl_traced")), _tick(r.get("sva_traced")), _tick(r.get("tc_traced")),
                    _tick(r.get("sim_pass")), _score(r.get("mutation_score")),
                    Text("signed off", "green") if r.get("signed_off") else Text("open", "yellow")))
    return out


def _rtm_detail(app, rid: str) -> str:
    r = next((x for x in _rtm_rows_data(app) if (x.get("req_id") or x.get("req")) == rid), None)
    if r is None:
        return ""
    lines = [f"### {rid}", "", short(r.get("text", ""), 400), ""]
    conds = [("1 RTL traced", r.get("rtl_traced"), ", ".join(r.get("rtl_files") or [])),
             ("2 SVA confirmed", r.get("sva_traced"), ", ".join(r.get("sva_assertions") or [])),
             ("3 TC selected", r.get("tc_traced"), ", ".join(r.get("tc_ids") or [])),
             ("4 Simulation passes", r.get("sim_pass"), f"{r.get('sva_violation_count', 0)} SVA violation(s)"),
             ("5 Mutation ≥ 85%", (r.get("mutation_score") >= 0.85) if isinstance(r.get("mutation_score"), (int, float)) else None,
              str(r.get("mutation_score", "pending"))),
             ("6 Signed off by you", r.get("signed_off"), r.get("sign_off_note") or "")]
    lines += [f"- {OK() if ok else (BAD() if ok is False else NA())} **{name}** {extra}" for name, ok, extra in conds]
    md = r.get("mutation_detail")
    if md:
        lines.append(f"\nmutants: {md}")
    return "\n".join(lines)


class RtmTab(DataTab):
    """The RTM dashboard: v signs a requirement off (refused unless conditions 1–5 hold) · V takes it back ·
    S signs off every requirement that is already ready."""

    BINDINGS = [Binding("v", "sign(True)", "Sign off"), Binding("V", "sign(False)", "Un-sign"),
                Binding("S", "sign_all", "Sign all ready")]

    def action_sign_all(self) -> None:
        try:
            self.app.log_info(self.capp.ops.sign_all_ready())  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            self.notify_error(exc)
        self.capp.refresh_all()

    def action_sign(self, signed: bool) -> None:
        rid = self.current_key()
        if rid is None:
            return

        def apply(note: str = "") -> None:
            try:
                self.app.log_info(self.capp.ops.sign_req(rid, signed, note))  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                self.notify_error(exc)
            self.capp.refresh_all()

        if signed:
            self.app.push_screen(self.capp.prompt_screen(f"Sign off {rid}", "Optional note (who / what was checked). Submit to sign off.",
                                                         multiline=False), lambda text: apply(text or "") if text is not None else None)
        else:
            apply()


def _tc_result_rows(app) -> list[tuple]:
    rep = artifact(app, "verification_report.json", {}) or {}
    res = (rep.get("sim_summary") or {}).get("tc_results") or {}
    if isinstance(res, dict):
        items = list(res.items())
    else:
        items = [(r.get("tc_id", "?"), r.get("result", r.get("status", ""))) for r in res if isinstance(r, dict)]
    return [(str(k), str(k), Text(str(v), "green" if v == "pass" else "red" if v in ("fail", "timeout") else "grey50")) for k, v in items]


def _verify_header(app) -> str:
    rep = artifact(app, "verification_report.json", {}) or {}
    sim, mut, rtm = rep.get("sim_summary", {}), rep.get("mutation_summary", {}), rep.get("rtm_summary", {})
    if not rep:
        return "no verification report yet"
    return (f"TC {sim.get('pass', 0)}/{sim.get('total_tc', 0)} pass · SVA violations {sim.get('sva_violations', 0)} · mutation "
            f"score {mut.get('global_score', 'n/a')} · REQ signed off {rtm.get('signed_off', 0)}/{rtm.get('total_req_ids', 0)}")


# -- registration -------------------------------------------------------------------------------------------------


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
    if kind == "vlsit_rtl":
        return (TabSpec("modules", "Modules", ("File", "Lines", "REQ tags", "Cells", "Area"), _rtl_rows, _rtl_detail,
                        "lint must be clean (0 warnings) and every requirement tagged `// REQ-xxx`", empty="no RTL yet — run (r)"),
                TabSpec("synth", "Synthesis", ("Item", "Value"), _synth_rows, empty="no synth_report.json yet"))
    if kind == "vlsit_tb":
        return (TabSpec("plan", "Test plan", ("TC", "Title", "REQs", "Selected"), _tc_rows, empty="no selected_testplan.json yet — run (r)"),
                TabSpec("gaps", "Uncovered REQs", ("REQ", "Category", "Text"), _uncovered_rows,
                        hint="requirements no selected test case covers — Gate 4 warns, never silently", urgent=_tb_urgent))
    if kind == "vlsit_sva":
        return (TabSpec("props", "Assertions", ("Label", "Module", "REQs", "Kind", "Not vacuous", "Review"), _sva_rows, _sva_detail,
                        "c confirm · x reject (with a reason) · u pending · C confirm all — only confirmed assertions count in the RTM",
                        urgent=_sva_urgent, widget=SvaTab, empty="no assertions yet — run (r)"),)
    if kind == "vlsit_verify":
        return (TabSpec("rtm", "RTM", ("REQ", "Category", "RTL", "SVA", "TC", "Sim", "Mutation", "Sign-off"), _rtm_rows, _rtm_detail,
                        "v sign off the highlighted requirement (needs RTL, SVA, TC, sim and mutation) · V take it back · S sign off every requirement that is ready",
                        urgent=_rtm_urgent, widget=RtmTab, empty="no RTM yet — run (r)"),
                TabSpec("sim", "Simulation", ("TC", "Result"), _tc_result_rows, empty="no verification_report.json yet"))
    return ()


def _builder(kind: str):
    def build(step: "StepDef"):
        return VlsitPanel(step, _tabs_for(kind))

    return build


for _kind in ("vlsit_parse", "vlsit_config", "vlsit_rtl", "vlsit_tb", "vlsit_sva", "vlsit_verify", "vlsit_doc"):
    register_panel(_kind, _builder(_kind))

HEADERS = {"vlsit_parse": _parse_header, "vlsit_verify": _verify_header}  # one-line summaries for the gate dialog
