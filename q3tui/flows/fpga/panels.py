"""TUI panels of the FPGA flow's step kinds (the flow's `panels.py`, imported by the TUI when it opens the flow; docs/spec/fpga-flow.md):
the SDC editor and the PPA report. Edits go through the flow's actions (`addon.py`), like `q3tui act` and the assistant."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import DataTable, Static

from q3tui.flows.fpga.lib import sdc as sdcmod
from q3tui.tui.panels import register_panel
from q3tui.tui.screens import EditScreen
from q3tui.tui.splitter import Splitter
from q3tui.tui.views import IdTable, Markdown, fill_table, set_markdown
from q3tui.tui.vlsit_panels import DataTab, TabSpec, VlsitPanel, artifact

if TYPE_CHECKING:
    from q3tui.pipeline.base import StepDef
    from q3tui.tui.app import Q3TUIApp

# -- SDC: one tab per section, like the files ---------------------------------------------------------------------------------


def _cfg(app: "Q3TUIApp"):
    return sdcmod.load(app.project)


def _file_detail(name: str, hint: str):
    """The detail pane of a section: its generated Tcl file and the keys."""

    def detail(app: "Q3TUIApp", key: str) -> str:
        f = app.project.root / "sdc" / name
        body = f.read_text() if f.is_file() else "(not generated yet)"
        return f"**`{key}`** → `sdc/{name}`\n\n```tcl\n{body}\n```\n\n{hint}"

    return detail


def _clock_rows(app: "Q3TUIApp") -> list[tuple]:
    cfg = _cfg(app)
    return [(f"clock.{c.name}", c.name, c.port, f"{c.freq_mhz:g}", f"{c.period_ns:g}", f"{c.uncertainty_ns:g}", f"{c.duty_pct:g}")
            for c in (cfg.clocks if cfg else [])]


def _group_rows(app: "Q3TUIApp") -> list[tuple]:
    cfg = _cfg(app)
    return [(f"group.{i}", g.kind, " | ".join(",".join(x) for x in g.groups)) for i, g in enumerate(cfg.clock_groups if cfg else [])]


def _keyed(app: "Q3TUIApp", prefix: str) -> list[tuple]:
    cfg = _cfg(app)
    return [(k, name, value or Text("–", style="grey50")) for k, name, value in (sdcmod.rows(cfg) if cfg else []) if k.startswith(prefix)]


def _io_rows(app: "Q3TUIApp") -> list[tuple]:
    return _keyed(app, "io.")


def _env_rows(app: "Q3TUIApp") -> list[tuple]:
    return _keyed(app, "env.")


def _exception_rows(app: "Q3TUIApp") -> list[tuple]:
    cfg = _cfg(app)
    if not cfg:
        return []
    rows: list[tuple] = [("resets", "false path", ", ".join(cfg.resets) or Text("–", style="grey50"), "–", "–", "asynchronous resets")]
    for i, e in enumerate(cfg.exceptions):
        rows.append((f"exception.{i}", e.kind, e.from_ or "–", e.to or "–", e.through or "–", f"{e.value:g}" if e.value is not None else (e.note or "")))
    return rows


class SdcEditor(Vertical):
    """The SDC step's view in three columns: the sections (clocks, clock groups, I/O, exceptions, environment), the items of the
    highlighted section, and the detail — the section's generated Tcl file. Every change is one of the flow's actions
    (addon.py), the same ones `q3tui act` and the assistant call. Tab moves between the columns; the bars between them are the
    app's splitters, as between the columns of the other step views (drag to resize, double-click to reset, remembered per project)."""

    DEFAULT_CSS = """
    SdcEditor > Horizontal { height: 1fr; }
    SdcEditor #sdc-sections { width: 24; }
    SdcEditor #sdc-items { width: 2fr; }
    SdcEditor VerticalScroll { width: 1fr; padding: 0 1; }
    SdcEditor .sdc-hint { height: auto; }
    """
    BINDINGS = [Binding("e", "edit", "Edit"), Binding("a", "add", "Add"), Binding("d", "remove", "Remove")]

    def __init__(self, tab=None, **kw):
        super().__init__(classes="refreshable", **kw)
        self.section = SECTIONS[0].id
        self._shown: str | None = None

    @property
    def capp(self) -> "Q3TUIApp":
        return self.app  # type: ignore[return-value]

    def compose(self) -> ComposeResult:
        yield Static("", classes="hint sdc-hint", id="sdc-hint")
        with Horizontal():
            yield IdTable(cursor_type="row", zebra_stripes=True, id="sdc-sections")
            yield Splitter(vertical=True, min_size=14, id="sdc-split-1")
            yield IdTable(cursor_type="row", zebra_stripes=True, id="sdc-items")
            yield Splitter(vertical=True, min_size=20, id="sdc-split-2")
            with VerticalScroll():
                yield Markdown()

    def on_mount(self) -> None:
        self.query_one("#sdc-sections", DataTable).add_columns("Section", "#")

    # -- what is shown ------------------------------------------------------------------------------------------------

    @property
    def spec(self) -> "Section":
        return next(s for s in SECTIONS if s.id == self.section)

    def _items(self) -> DataTable:
        return self.query_one("#sdc-items", DataTable)

    def current_key(self) -> str | None:
        t = self._items()
        return t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value if t.row_count else None

    def refresh_view(self) -> None:
        app, sec = self.capp, self.spec
        fill_table(self.query_one("#sdc-sections", DataTable), [(s.id, s.title, str(len(s.rows(app)))) for s in SECTIONS])
        items = self._items()
        if self._shown != sec.id:  # another section: its own columns
            items.clear(columns=True)
            items.add_columns(*sec.columns)
            items._q3tui_rows = None  # type: ignore[attr-defined]
            self._shown = sec.id
        rows = sec.rows(app)
        fill_table(items, rows)
        self.query_one("#sdc-hint", Static).update(f"{sec.hint} · Tab: next column · drag a bar to resize")
        self._detail(rows)

    def _detail(self, rows: list | None = None) -> None:
        sec, key = self.spec, self.current_key()
        if not (rows if rows is not None else sec.rows(self.capp)):
            text = f"_{sec.empty}_\n\n" + sec.detail(self.capp, sec.id)
        else:
            text = sec.detail(self.capp, key or sec.id)
        set_markdown(self.query_one(Markdown), text)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        event.stop()
        if event.data_table.id == "sdc-sections":
            if event.row_key.value and event.row_key.value != self.section:
                self.section = event.row_key.value
                self.refresh_view()
        else:
            self._detail()

    # -- changes: always one of the flow's actions ----------------------------------------------------------------------

    def _act(self, name: str, **kw) -> None:
        try:
            self.capp.log_info(self.capp.ops.run_action(name, **kw))
        except Exception as exc:  # noqa: BLE001 - friendly message, never a traceback
            self.app.notify(str(exc), severity="error")
        self.capp.refresh_all()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter on an item edits it."""
        event.stop()
        if event.data_table.id == "sdc-items":
            self.action_edit()

    # -- the dialog: the fields of the highlighted item (or of a new one) -----------------------------------------------

    def _fields(self, new: bool) -> tuple[str, list[tuple], Callable[[dict], None]] | None:
        """(title, EditScreen fields, apply(values)) for the highlighted item, or for a new one in this section."""
        sec, key, cfg = self.spec.id, self.current_key(), _cfg(self.capp)
        if cfg is None:
            return None
        set_ = lambda k, v: self._act("sdc_set", key=k, value=v)  # noqa: E731
        changed = lambda values, old: {k: v.strip() for k, v in values.items() if v.strip() != str(old.get(k, "")).strip()}  # noqa: E731
        if sec == "clocks":
            if new:
                return ("Add a clock", [("name", "Name", "", "line"), ("port", "Port", "", "line"), ("mhz", "Frequency (MHz)", "100", "line")],
                        lambda v: self._act("sdc_add", what="clock", text=f"{v['name'].strip()} {v['port'].strip()} {round(1000 / float(v['mhz']), 4)}"))
            c = next((c for c in cfg.clocks if f"clock.{c.name}" == key), None)
            if c is None:
                return None
            old = {"port": c.port, "freq_mhz": f"{c.freq_mhz:g}", "uncertainty_ns": f"{c.uncertainty_ns:g}", "duty_pct": f"{c.duty_pct:g}"}
            fields = [("port", "Port", old["port"], "line"), ("freq_mhz", "Frequency (MHz)", old["freq_mhz"], "line"),
                      ("uncertainty_ns", "Uncertainty (ns)", old["uncertainty_ns"], "line"), ("duty_pct", "Duty cycle (%)", old["duty_pct"], "line")]
            return (f"Clock {c.name}", fields, lambda v: [set_(f"clock.{c.name}.{k}", x) for k, x in changed(v, old).items()])
        if sec == "groups":
            g = next((g for i, g in enumerate(cfg.clock_groups) if f"group.{i}" == key), None) if not new else None
            if g is None and not new:
                return None
            kinds = ["asynchronous", "logically_exclusive", "physically_exclusive"]
            fields = [("kind", "Kind", g.kind if g else kinds[0], "choice", kinds),
                      ("groups", "Groups: clocks of each, separated by |  (clkA | clkB)", " | ".join(",".join(x) for x in g.groups) if g else "", "line")]
            text = lambda v: f"{v['kind']} {v['groups'].strip()}"  # noqa: E731
            return ("Clock group", fields, (lambda v: self._act("sdc_add", what="group", text=text(v))) if new
                    else (lambda v: self._act("sdc_replace", key=key, text=text(v))))
        if sec in ("io", "env"):
            if new:
                return None
            prefix = "io." if sec == "io" else "env."
            rows = [r for r in sdcmod.rows(cfg) if r[0].startswith(prefix)]
            real = {r[0].replace(".", "_"): r[0] for r in rows}  # (a widget id cannot hold a dot)
            old = {r[0].replace(".", "_"): r[2] for r in rows}
            fields = [(r[0].replace(".", "_"), r[1], r[2], "line") for r in rows]
            return ("I/O delay budget" if sec == "io" else "Environment", fields,
                    lambda v: [set_(real[k], x) for k, x in changed(v, old).items()])
        if sec == "exceptions":
            if not new and key == "resets":
                return ("Asynchronous resets", [("resets", "Reset inputs (false paths), separated by commas", ", ".join(cfg.resets), "line")],
                        lambda v: set_("resets", v["resets"]))
            e = next((e for i, e in enumerate(cfg.exceptions) if f"exception.{i}" == key), None) if not new else None
            if e is None and not new:
                return None
            kinds = ["false_path", "multicycle", "max_delay", "min_delay"]
            fields = [("kind", "Kind", e.kind if e else kinds[0], "choice", kinds), ("from", "From (clock or port)", e.from_ if e else "", "line"),
                      ("to", "To (clock or port)", e.to if e else "", "line"), ("through", "Through (pin)", e.through if e else "", "line"),
                      ("value", "Value (cycles for multicycle, ns for min / max delay)", "" if e is None or e.value is None else f"{e.value:g}", "line"),
                      ("note", "Note", e.note if e else "", "line")]

            def text(v: dict) -> str:
                parts = [v["kind"]] + [f"{k}={v[k].strip()}" for k in ("from", "to", "through", "value") if v[k].strip()]
                return " ".join(parts) + (f" {v['note'].strip()}" if v["note"].strip() else "")

            return ("Path exception", fields, (lambda v: self._act("sdc_add", what="exception", text=text(v))) if new
                    else (lambda v: self._act("sdc_replace", key=key, text=text(v))))
        return None

    def _dialog(self, new: bool) -> None:
        form = self._fields(new)
        if form is None:
            self.app.notify("nothing to edit here" if not new else f"nothing to add in {self.spec.title}", severity="information")
            return
        title, fields, apply = form

        def done(values: dict | None) -> None:
            if values is None:
                return
            try:
                apply(values)
            except (ValueError, ZeroDivisionError) as exc:  # a number that is not one (the actions report their own errors)
                self.app.notify(str(exc), severity="error")

        self.capp.push_screen(EditScreen(title, fields, "ctrl+s to apply · Esc to cancel · an empty optional field clears it"), done)

    def action_edit(self) -> None:
        self._dialog(new=False)

    def action_add(self) -> None:
        self._dialog(new=True)

    def action_remove(self) -> None:
        key = self.current_key()
        if key and key != "resets":
            self._act("sdc_remove", key=key)


@dataclass
class Section:
    id: str
    title: str
    columns: tuple[str, ...]
    rows: Callable[["Q3TUIApp"], list[tuple]]
    detail: Callable[["Q3TUIApp", str], str]
    hint: str
    empty: str = "nothing here"


SECTIONS = (
    Section("clocks", "Clocks", ("Clock", "Port", "MHz", "Period ns", "Uncert. ns", "Duty %"), _clock_rows,
            _file_detail("clocks.tcl", "`e` edit · `a` add a clock · `d` remove"),
            "e edit · a add a clock · d remove", "no sdc/sdc.json yet — run (r)"),
    Section("groups", "Clock groups", ("Kind", "Groups"), _group_rows, _file_detail("clock_groups.tcl", "`e` edit · `a` add · `d` remove"),
            "relations between clocks (asynchronous / exclusive); a single clock needs none · e edit · a add · d remove",
            "no clock groups: a single clock domain needs none"),
    Section("io", "I/O", ("Setting", "Value"), _io_rows, _file_detail("io_constraints.tcl", "`e` edit"),
            "input / output delay = a share of the clock period on every port except clocks, resets and the excluded ones · e edit",
            "no sdc/sdc.json yet — run (r)"),
    Section("exceptions", "Exceptions", ("Kind", "From", "To", "Through", "Value"), _exception_rows,
            _file_detail("exceptions.tcl", "`e` edit · `a` add · `d` remove"),
            "false paths from asynchronous resets, multicycle and min / max delay paths · e edit · a add · d remove",
            "no sdc/sdc.json yet — run (r)"),
    Section("env", "Environment", ("Setting", "Value"), _env_rows, _file_detail("env.tcl", "`e` edit"),
            "driving cell, load, max fanout / transition — ASIC tools use them, the FPGA tools skip env.tcl · e edit",
            "no sdc/sdc.json yet — run (r)"),
)


# -- PPA report --------------------------------------------------------------------------------------------------


def _ppa(app: "Q3TUIApp") -> dict:
    return artifact(app, "ppa_report.json", {}) or {}


def _ppa_rows(app: "Q3TUIApp") -> list[tuple]:
    d = _ppa(app)
    if not d:
        return []
    t, v, tg = d.get("timing", {}), d.get("verdict", {}), d.get("targets", {}).get("max_util_pct", {})
    ok, bad = Text("pass", style="green"), Text("fail", style="red")
    rows = [("part", "Part", f"{d.get('part')} ({d.get('stage')})", "", ""),
            ("wns", "Setup slack WNS (ns)", f"{t.get('wns_ns')}", f"≥ {d.get('targets', {}).get('min_slack_ns', 0)}", ok if v.get("timing") == "pass" else bad),
            ("tns", "Total negative slack TNS (ns)", f"{t.get('tns_ns')}", "", ""),
            ("whs", "Hold slack WHS (ns)", f"{t.get('whs_ns')}", "judged after implementation" if v.get("hold", "").startswith("not") else "≥ 0",
             Text(v.get("hold", ""), style="grey50") if v.get("hold", "").startswith(("not", "n/a")) else (ok if v.get("hold") == "pass" else bad)),
            ("fmax", "Fmax estimate (MHz)", f"{t.get('fmax_mhz')}", ", ".join(f"{c['name']} {c['freq_mhz']:g}" for c in d.get("clocks", [])), "")]
    for k, u in d.get("utilization", {}).items():
        if k in ("lut", "ff", "bram_tile", "dsp", "lut_ram", "io", "uram"):
            budget = tg.get(k)
            rows.append((f"u_{k}", f"{k.upper()} used", f"{u['used']} / {u['available']} ({u['pct']}%)", f"≤ {budget}%" if budget else "",
                         (bad if k in v.get("over_budget", {}) else ok) if budget else ""))
    cp = d.get("critical_path") or {}
    if cp:
        rows.append(("cp", "Worst path", f"{cp.get('source', '')} → {cp.get('destination', '')}",
                     f"{cp.get('logic_levels', '?')} levels", f"{cp.get('slack_ns', '')} ns"))
    for k, n in (d.get("unconstrained") or {}).items():
        rows.append((f"c_{k}", f"check_timing: {k}", str(n), "", Text("look", style="yellow")))
    return rows


def _ppa_detail(app: "Q3TUIApp", key: str) -> str:
    d = _ppa(app)
    if key == "cp":
        f = app.project.root / "fpga" / "reports" / "paths.rpt"
        return f"```\n{f.read_text(errors='replace')[:6000]}\n```" if f.is_file() else "_no paths.rpt_"
    return f"Overall: **{d.get('verdict', {}).get('overall', '?')}** — post-synthesis estimates; placement and routing can still change them."


# -- PPA optimization ----------------------------------------------------------------------------------------------


def _plan(app: "Q3TUIApp") -> dict:
    return artifact(app, "ppa_plan.json", {}) or {}


def _fix_rows(app: "Q3TUIApp") -> list[tuple]:
    p = _plan(app)
    rows = []
    for f in p.get("fixes", []):
        sent = f.get("handed_off")
        rows.append((f["id"], f["id"], f["target"], f.get("module") or "–", f["change"][:90],
                     Text(f"→ {sent['flow']}:{sent['step']}", style="green") if sent else Text("not yet", style="yellow")))
    return rows


def _fix_detail(app: "Q3TUIApp", key: str) -> str:
    p = _plan(app)
    f = next((x for x in p.get("fixes", []) if x["id"] == key), None)
    if f is None:
        return ""
    return (f"**{f['id']}** — {f['target']}{(' · `' + f['module'] + '`') if f.get('module') else ''}\n\n{f['change']}\n\n"
            f"- Evidence: {f.get('why', '')}\n- Expected: {f.get('expected', '')}\n- Risk: {f.get('risk', '')}\n\n"
            f"_Analysis:_ {p.get('analysis', '')}")


class FixTab(DataTab):
    """The proposed fixes: h hands every fix not sent yet (or the highlighted one: H) to the flow that owns the RTL / the spec."""

    BINDINGS = [Binding("h", "handoff", "Hand off all"), Binding("H", "handoff_one", "Hand off this")]

    def _hand(self, ids: str) -> None:
        try:
            self.capp.log_info(self.capp.ops.run_action("ppa_handoff", ids=ids))
        except Exception as exc:  # noqa: BLE001
            self.notify_error(exc)
        self.capp.refresh_all()

    def action_handoff(self) -> None:
        self._hand("")

    def action_handoff_one(self) -> None:
        key = self.current_key()
        if key:
            self._hand(key)


def _tabs_for(kind: str) -> tuple[TabSpec, ...]:
    if kind == "fpga.sdc":
        return (TabSpec("sdc", "SDC", (), lambda app: [], widget=SdcEditor),)
    if kind == "fpga.timing_util_report":
        return (TabSpec("ppa", "PPA", ("Item", "Value", "Target", "Status"), _ppa_rows, _ppa_detail,
                        "set targets in the flow file: options.targets.max_util_pct {lut: 70}, min_slack_ns", empty="no ppa_report.json yet — run (r)"),)
    if kind == "fpga.ppa_optimize":
        return (TabSpec("fixes", "Fixes", ("Fix", "Target", "Module", "Change", "Handed off"), _fix_rows, _fix_detail,
                        "h hand every fix to the flow that owns the RTL / spec (H: the highlighted one) — then run that flow to apply them",
                        widget=FixTab, empty="no proposal yet — it runs when a target is missed (run r)"),)
    return ()


def _builder(kind: str):
    def build(step: "StepDef"):
        return VlsitPanel(step, _tabs_for(kind))

    return build


for _kind in ("fpga.sdc", "fpga.timing_util_report", "fpga.ppa_optimize"):
    register_panel(_kind, _builder(_kind))
