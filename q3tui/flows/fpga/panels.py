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

from q3tui.flows.fpga.lib import hints
from q3tui.flows.fpga.lib import sdc as sdcmod
from q3tui.tui.panels import register_panel
from q3tui.tui.screens import EditScreen
from q3tui.tui.splitter import Splitter
from q3tui.tui.views import short, IdTable, Markdown, fill_table, set_markdown
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
        f = app.project.sdc_dir / name
        body = f.read_text() if f.is_file() else "(not generated yet)"
        return f"**`{key}`** → `sdc/{name}`\n\n```tcl\n{body}\n```\n\n{hint}"

    return detail


def _asic_cell(hint: dict | None) -> Text:
    """What the ASIC flow's last synthesis says this clock could run at (the library's limit), or – when it has not run."""
    if not hint or not hint.get("fmax_mhz"):
        return Text("–", style="grey50")
    ok = hint["fmax_mhz"] >= (hint.get("target_mhz") or 0)
    return Text(("≥ " if hint.get("bound") == "at_least" else "") + ("~" if hint.get("source") == "estimate" else "") + f"{hint['fmax_mhz']:g}" + (" *" if hint.get("stale") else ""), style="green" if ok else "yellow")


def _clock_rows(app: "Q3TUIApp") -> list[tuple]:
    cfg = _cfg(app)
    asic = hints.asic_hint(app.project)
    return [(f"clock.{c.name}", c.name, c.port, f"{c.freq_mhz:g}",
             f"{c.asic_freq_mhz:g}" if c.asic_freq_mhz else Text("same", style="grey50"), f"{c.period_ns:g}", f"{c.uncertainty_ns:g}", f"{c.duty_pct:g}", _asic_cell(asic.get(c.name)))
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
    BINDINGS = [Binding("e", "edit", "Edit"), Binding("a", "add", "Add"), Binding("d", "remove", "Remove"),
                Binding("E", "estimate", "ASIC estimate")]

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

    def _act_settings(self, changes: dict) -> None:
        """A setting both flows share (the ASIC library): applied and saved through Ops, like Settings does."""
        try:
            self.capp.log_info(self.capp.ops.apply_settings(changes, save=True))
        except Exception as exc:  # noqa: BLE001
            self.app.notify(str(exc), severity="error")
        self.capp.refresh_all()

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
            old = {"port": c.port, "freq_mhz": f"{c.freq_mhz:g}", "uncertainty_ns": f"{c.uncertainty_ns:g}", "duty_pct": f"{c.duty_pct:g}",
                   "asic_freq_mhz": "" if c.asic_freq_mhz is None else f"{c.asic_freq_mhz:g}"}
            fields = [("port", "Port", old["port"], "line"), ("freq_mhz", "Frequency (MHz)", old["freq_mhz"], "line"),
                      ("uncertainty_ns", "Uncertainty (ns)", old["uncertainty_ns"], "line"), ("duty_pct", "Duty cycle (%)", old["duty_pct"], "line"),
                      ("asic_freq_mhz", "ASIC frequency (MHz) — the ASIC flow's own target; empty: the same as above", old["asic_freq_mhz"], "line")]
            from q3tui.flows.asic.lib import pdks

            cur = hints.asic_library(self.capp.project)
            techs = pdks.catalog(self.capp.project)
            found = pdks.locate(cur, self.capp.project)
            by_title = {t.label: t for t in techs}
            if found:  # a catalogued library: technology, then corner (the corners follow the technology)
                tech, corner = found
                fields.append(("asic_pdk", "ASIC technology — used by the estimate and the ASIC synthesis", tech.label, "choice", list(by_title)))
                fields.append(("asic_corner", "ASIC library corner", corner, "choice", [c for c, _p in tech.corners]))
            else:  # (a path of yours: Settings or here)
                fields.append(("asic_lib", "ASIC library (.db) — used by the estimate and the ASIC synthesis", cur, "line"))

            def pick_tech(screen, title: str) -> None:
                t = by_title.get(title)
                if t is not None:
                    default = next((c for c, p in t.corners if p == t.default_path()), t.corners[0][0])
                    screen.set_choices("asic_corner", [c for c, _p in t.corners], default)

            def set_library(values: dict) -> bool:
                """The library of the form, saved for both flows when it changed (before an estimate, so the button answers for it)."""
                if "asic_pdk" in values:
                    t = by_title.get(str(values.get("asic_pdk")))
                    chosen = dict(t.corners).get(str(values.get("asic_corner")), "") if t else ""
                else:
                    chosen = str(values.get("asic_lib") or "").strip()
                if chosen and chosen != cur:
                    self._act_settings({hints.LIBRARY_OPTION: chosen})
                    return True
                return False

            def estimate(screen) -> None:  # a button of the form: the ASIC library's Fmax for this RTL, answered in the form
                set_library(screen._values())
                self.action_estimate(screen.set_status)

            return (f"Clock {c.name}", fields, lambda v: [set_(f"clock.{c.name}.{k}", x) for k, x in changed(v, {**old, "asic_lib": "", "asic_pdk": "", "asic_corner": ""}).items()
                                       if k not in ("asic_lib", "asic_pdk", "asic_corner")] + [set_library(v)],
                    [("estimate", "Estimate ASIC Fmax", estimate)], {"asic_pdk": pick_tech})
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
        title, fields, apply, *extra = form  # (+ the form's buttons and the fields that depend on another)

        def done(values: dict | None) -> None:
            if values is None:
                return
            try:
                apply(values)
            except (ValueError, ZeroDivisionError) as exc:  # a number that is not one (the actions report their own errors)
                self.app.notify(str(exc), severity="error")

        self.capp.push_screen(EditScreen(title, fields, "ctrl+s to apply · Esc to cancel · an empty optional field clears it", extra[0] if extra else (),
                                           extra[1] if len(extra) > 1 else None), done)

    def action_edit(self) -> None:
        self._dialog(new=False)

    def action_add(self) -> None:
        self._dialog(new=True)

    def action_remove(self) -> None:
        key = self.current_key()
        if key and key != "resets":
            self._act("sdc_remove", key=key)

    def action_estimate(self, status=None) -> None:
        """E (or the button of a clock's dialog): estimate the clock the ASIC library allows (about a minute, in a thread: the app stays alive);
        the column and the log show it, and `status(text)` — the dialog's status line — says it too."""
        import anyio

        from q3tui.flows.asic.lib import estimate

        app = self.capp
        say = status or (lambda text: None)
        say("Estimating with Design Compiler (low effort, about a minute)…")
        app.notify("Estimating the ASIC clock with Design Compiler (about a minute)…")

        async def go() -> None:
            try:
                msg = await anyio.to_thread.run_sync(lambda: estimate.compute(app.ops))
            except Exception as exc:  # noqa: BLE001 - friendly message, never a traceback
                app.notify(str(exc), severity="error")
                say(f"Not estimated: {exc}")
                return
            app.log_info(app.ops._log(next((s.name for s in app.engine.steps if s.kind.endswith(".sdc")), app.engine.steps[0].name), msg))
            app.refresh_all()
            say(msg + "\nSet the ASIC frequency at or below it (the estimate leans a little low), then ctrl+s.")

        self.run_worker(go(), exclusive=True, group="asic-estimate")


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
    Section("clocks", "Clocks", ("Clock", "Port", "MHz", "ASIC MHz", "Period ns", "Uncert. ns", "Duty %", "ASIC Fmax MHz"), _clock_rows,
            _file_detail("clocks.tcl", "`e` edit · `a` add a clock · `d` remove"),
            "e edit · a add a clock · d remove · ASIC MHz: the ASIC flow's own target for the clock (e; same = the FPGA's) · ASIC Fmax: what the library can reach for this clock — E estimates it in about a minute (~), the ASIC flow's synthesis is exact (≥ = it met the clock it was given and can go faster; * = RTL changed since; – = not run)", "no constraints/sdc/sdc.json yet — run (r)"),
    Section("groups", "Clock groups", ("Kind", "Groups"), _group_rows, _file_detail("clock_groups.tcl", "`e` edit · `a` add · `d` remove"),
            "relations between clocks (asynchronous / exclusive); a single clock needs none · e edit · a add · d remove",
            "no clock groups: a single clock domain needs none"),
    Section("io", "I/O", ("Setting", "Value"), _io_rows, _file_detail("io_constraints.tcl", "`e` edit"),
            "input / output delay = a share of the clock period on every port except clocks, resets and the excluded ones · e edit",
            "no constraints/sdc/sdc.json yet — run (r)"),
    Section("exceptions", "Exceptions", ("Kind", "From", "To", "Through", "Value"), _exception_rows,
            _file_detail("exceptions.tcl", "`e` edit · `a` add · `d` remove"),
            "false paths from asynchronous resets, multicycle and min / max delay paths · e edit · a add · d remove",
            "no constraints/sdc/sdc.json yet — run (r)"),
    Section("env", "Environment", ("Setting", "Value"), _env_rows, _file_detail("env.tcl", "`e` edit"),
            "driving cell, load, max fanout / transition — ASIC tools use them, the FPGA tools skip env.tcl · e edit",
            "no constraints/sdc/sdc.json yet — run (r)"),
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
        if k in ("lut", "ff", "bram_tile", "dsp", "lut_ram", "io", "uram", "mem_bits"):
            budget = tg.get(k)
            value = f"{u['used']} / {u['available']} ({u['pct']}%)" if u.get("available") is not None else f"{u['used']}"  # (Quartus: no total for some)
            rows.append((f"u_{k}", f"{u.get('label') or k.upper()} used", value, f"≤ {budget}%" if budget else "",
                         (bad if k in v.get("over_budget", {}) else ok) if budget else ""))
    pl, j = v.get("plausibility"), v.get("judgment") or {}
    if pl:
        rows.append(("plaus", "Is it the design?", short(j.get("summary") or "", 70), short(j.get("expected") or "", 40),
                     ok if pl == "pass" else bad if pl == "fail" else Text(pl, style="grey50" if pl == "n/a" else "yellow")))
    cp = d.get("critical_path") or {}
    if cp:
        rows.append(("cp", "Worst path", f"{cp.get('source', '')} → {cp.get('destination', '')}",
                     f"{cp.get('logic_levels', '?')} levels", f"{cp.get('slack_ns', '')} ns"))
    for k, n in (d.get("unconstrained") or {}).items():
        rows.append((f"c_{k}", f"check_timing: {k}", str(n), "", Text("look", style="yellow")))
    return rows


def _ppa_detail(app: "Q3TUIApp", key: str) -> str:
    d = _ppa(app)
    if key == "plaus":
        j = (d.get("verdict") or {}).get("judgment") or {}
        lines = [f"**Is it the design?** {(d.get('verdict') or {}).get('plausibility')} ({'judged by the LLM' if j.get('by') == 'llm' else 'by code'})", "",
                 j.get("summary") or ""]
        if j.get("expected"):
            lines += ["", f"Expected: {j['expected']}"]
        if j.get("why"):
            lines += ["", "Why it was judged:", *(f"- {w}" for w in j["why"])]
        for c in j.get("causes") or []:
            lines += ["", f"- **{c.get('owner')}{':' + c['module'] if c.get('module') else ''}**: {c.get('what')}",
                      f"  - evidence: {c.get('evidence')}", f"  - fix: {c.get('request') or '–'}"]
        return "\n".join(lines)
    if key == "cp":
        f = app.project.fpga_dir / "reports" / "paths.rpt"
        return f"```\n{f.read_text(errors='replace')[:6000]}\n```" if f.is_file() else "_no paths.rpt_"
    return f"Overall: **{d.get('verdict', {}).get('overall', '?')}** — post-synthesis estimates; placement and routing can still change them."


# -- PPA optimization ----------------------------------------------------------------------------------------------


def _plan(app: "Q3TUIApp") -> dict:
    return artifact(app, "ppa_plan.json", {}) or {}


def _port(app: "Q3TUIApp") -> dict:
    from q3tui.core.project import read_json
    from q3tui.flows.fpga.lib.port import PORT_JSON

    return read_json(app.project.schemas_dir / PORT_JSON, {}) or {}


def _port_rows(app: "Q3TUIApp") -> list[tuple]:
    """What the scan found, the problems handed back, what the porter did, the RTL work orders (the newest round)."""
    p = _port(app)
    rows = [(f"scan.{i}", Text(x["rule"], style="yellow"), f"{x['file']}:{x['line']}", short(x["note"], 110)) for i, x in enumerate(p.get("scan") or [])]
    rows += [(f"req.{i}", Text("handed back", style="cyan"), "–", short(r, 110)) for i, r in enumerate(p.get("requests") or [])]
    rows += [(f"done.{i}", Text("done", style="green"), "src/fpga" if "src/fpga" in d else "setup", short(d, 110)) for i, d in enumerate(p.get("done") or [])]
    handed = p.get("handed") or []
    rows += [(f"order.{i}", Text("RTL order", style="magenta"), o["module"], short(o["change"], 90) + (f"  → {handed[i].split(' → ')[-1]}" if i < len(handed) else ""))
             for i, o in enumerate(p.get("orders") or [])]
    return rows


def _port_detail(app: "Q3TUIApp", key: str) -> str:
    p = _port(app)
    kind, _, i = key.partition(".")
    head = f"**{p.get('part', '?')}** — {p.get('tool', '')}\n\n_{p.get('summary', '')}_\n\n"
    try:
        n = int(i)
        if kind == "scan":
            x = p["scan"][n]
            return head + f"**{x['rule']}** `{x['file']}:{x['line']}` ({x['module']})\n\n```\n{x['code']}\n```\n\n{x['note']}"
        if kind == "req":
            return head + "**Handed back by the synthesis / the report**\n\n```\n" + p["requests"][n] + "\n```"
        if kind == "done":
            return head + f"**Done** {p['done'][n]}"
        if kind == "order":
            o = p["orders"][n]
            return head + f"**RTL order** `{o['module']}`\n\n{o['change']}\n\n- Why: {o['why']}"
    except (KeyError, IndexError, ValueError):
        pass
    return head


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
        app = self.capp
        try:
            app.log_info(app.ops.run_action("ppa_handoff", ids=ids))
        except Exception as exc:  # noqa: BLE001
            self.notify_error(exc)
            app.refresh_all()
            return
        app.refresh_all()
        step = next((s for s in app.engine.steps if s.kind == "fpga.ppa_optimize"), None)  # on to the flow that got the change requests
        app.action_switch_flow(str((step.options if step else {}).get("handoff_flow") or "rtl"))

    def action_handoff(self) -> None:
        self._hand("")

    def action_handoff_one(self) -> None:
        key = self.current_key()
        if key:
            self._hand(key)


# -- FPGA synthesis: the device and the synthesis options of the step, picked here (the Settings dialog has them too) ------------------------
# (the part decides the family and the tool: lib/templates.py `resolve`; the project flow's FPGA view lists the parts)

_SYNTH = "pipeline.step_options.fpga_synth."


def _synth_step(app: "Q3TUIApp"):
    return next((s for s in app.engine.steps if s.kind == "fpga.fpga_synth"), None)


def _synth_rows(app: "Q3TUIApp") -> list[tuple]:
    from q3tui.flows.fpga.lib import templates

    step = _synth_step(app)
    if step is None:
        return []
    o = step.options
    t, dev, note = templates.resolve(o)
    rows = [("part", "Device (part)", dev, note or ""),
            ("family", "Family and tool", t.label if t else "–", "from the part" if t else "no family here synthesizes this part")]
    if t is None:
        return [(k, n, v, Text(m, style="yellow")) for k, n, v, m in rows]
    if t.backend != "yosys":
        rows.append(("implement", "Place and route" + (" (fit)" if t.backend == "quartus" else ""), "on" if step._fpga_cfg().get("implement") else "off", ""))
    else:
        rows += [("package", "Package", str(o.get("package") or t.devices.get(dev, "")), "" if o.get("package") else "default of the device")]
        if t.speed:
            rows.append(("speed", "Speed grade", str(o.get("speed") or t.speed), "" if o.get("speed") else "default"))
        rows.append(("frontend", "Verilog frontend", str(o.get("frontend") or "verilog"), ""))
    rows.append(("synth_args", "Extra synthesis arguments", " ".join(map(str, step._fpga_cfg().get("synth_args") or [])) or "–", ""))
    return [(k, n, v, Text(m, style="yellow" if ("no " in m or "chosen" in m) else "grey50")) for k, n, v, m in rows]


def _synth_detail(app: "Q3TUIApp", key: str) -> str:
    from q3tui.flows.fpga.lib import templates

    step = _synth_step(app)
    if step is None:
        return ""
    t, dev, note = templates.resolve(step.options)
    if key in ("part", "family"):
        fams = "\n".join(f"- **{x.label}**: {x.help or 'part ' + x.part}" for x in templates.TEMPLATES.values())
        return (f"**Device** `{dev}`" + (f" — {t.label}" if t else "") + (f"\n\n{note}" if note else "") + "\n\nThe part decides the family and the tool. Enter picks one from "
                f"the device list (built in, yours, your spec's; Other… types one); the project flow's **FPGA** view adds yours (`a`).\n\nFamilies:\n{fams}")
    return f"**{key}** — Enter to change." + (f" {t.help}" if t else "")


class SynthTab(DataTab):
    """Enter on a row changes it: the part (its family follows), the options; saved to q3tui.yaml."""

    BINDINGS = [Binding("e", "edit", "Edit")]

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        self.action_edit()

    def action_edit(self) -> None:
        from q3tui.flows.fpga.lib import templates

        key, step = self.current_key(), _synth_step(self.capp)
        if not key or step is None:
            return
        o = step.options
        t, dev, _note = templates.resolve(o)
        title = dict((k, n) for k, n, *_ in _synth_rows(self.capp))[key]
        if key == "family":
            self.app.notify("the family follows the part: change the part (Enter on it, or the project flow's FPGA view)", severity="information")
            return
        if key == "part":
            self.capp.push_screen(*_part_picker(self.capp, lambda part: self._apply("part", part)))
            return
        if key == "implement":
            field, back = ("value", title, "on" if step._fpga_cfg().get("implement") else "off", "choice", ["off", "on"]), lambda v: v
        elif key == "frontend":
            field, back = ("value", title, str(o.get("frontend") or "verilog"), "choice", ["verilog", "slang"]), lambda v: v
        elif key == "synth_args":
            field, back = ("value", f"{title} — one per line", "\n".join(map(str, step._fpga_cfg().get("synth_args") or [])), "text"), lambda v: v
        else:  # package, speed
            field, back = ("value", f"{title} — empty: the default", str(o.get(key) or ""), "line"), lambda v: v

        def done(values: dict | None) -> None:
            if values is not None:
                self._apply(key, back(str(values["value"]).strip() if field[3] != "text" else values["value"]))

        self.capp.push_screen(EditScreen(f"FPGA synthesis — {title}", [field], "ctrl+s to apply · Esc to cancel · run (r) to synthesize again"), done)

    def _apply(self, key: str, value) -> None:
        try:
            self.capp.log_info(self.capp.ops.apply_settings({_SYNTH + key: value}, save=True))
        except Exception as exc:  # noqa: BLE001 - friendly message, never a traceback
            self.app.notify(str(exc), severity="error")
        self.capp.refresh_all()
        self.call_after_refresh(self._cursor_to, key)

    def _cursor_to(self, key: str) -> None:
        """Back on the row changed (another family's part adds its options' rows, and a new row takes the cursor)."""
        table = self.query_one(DataTable)
        try:
            table.move_cursor(row=table.get_row_index(key))
        except Exception:  # noqa: BLE001 - (the row went: the cursor stays where it is)
            pass


def _part_picker(app: "Q3TUIApp", use_part: Callable[[str], None]) -> tuple:
    """The device list to pick the part from (lib/devices.py, the project flow's FPGA view's list): the one in use first, then your spec's,
    yours, the built-in ones, each with its family's tool; Other… types a part. Parts no family here synthesizes are said, not offered."""
    from q3tui.flows.fpga.lib import devices, templates
    from q3tui.tui.picker import Choice, ChoiceScreen

    use = devices.in_use(app.project)
    order = {"spec": 0, "yours": 1, "built-in": 2}

    def in_use(e: dict) -> bool:
        return e["template"] == use["template"] and e["device"].lower() == use["device"].lower()

    choices, left_out = [], []
    for e in sorted(devices.entries(app.project), key=lambda e: (not in_use(e), min(order.get(s, 3) for s in e["sources"]), e["vendor"], e["part"])):
        t = templates.TEMPLATES.get(e["template"] or "")
        if t is None:
            left_out += [] if "spec" in e["sources"] else [f"{e['part']} ({e['note']})"]  # (the spec's are among the problems below)
            continue
        tags = [x for x, s in (("your spec", "spec"), ("yours", "yours")) if s in e["sources"]]
        choices.append(Choice(e["part"], f"{e['part']:<18} {e['title']} · {t.tool}" + (f" · {', '.join(tags)}" if tags else "") + (" — in use" if in_use(e) else ""),
                              style="green" if in_use(e) else ""))
    fam = templates.TEMPLATES[use["template"]].label if use["template"] else "no family here"
    body = "\n".join([f"In use: {use['device']} ({fam})" + ("" if use["set"] else " — not chosen: the default") + ". The family and the tool follow the "
                      "part; saved to q3tui.yaml. Add your own parts in the project flow's FPGA view (a).",
                      *(f"! {p}" for p in devices.problems(app.project) if use["set"] or p != use["note"]), *([f"Not offered (no family here synthesizes them): {', '.join(left_out)}"] if left_out else [])])

    def done(result) -> None:
        part = (result.text if result is not None and result.text is not None else (result.value if result is not None else None)) or ""
        if part.strip():
            use_part(part.strip())

    return ChoiceScreen("FPGA synthesis — Device (part)", choices, body=body, other="Other… (type a part)",
                        other_title="Device (part): xc7a100tcsg324-1, LFE5U-85F, up5k, 10AX115N2F40E2LG, …", other_value=str(use["part"] or "")), done


def _tabs_for(kind: str) -> tuple[TabSpec, ...]:
    if kind == "fpga.fpga_synth":
        return (TabSpec("fpga", "FPGA", ("Setting", "Name", "Value", "Note"), _synth_rows, _synth_detail,
                        "Enter: choose the device (its family and tool follow) and the synthesis options (saved to q3tui.yaml) — then run (r)", widget=SynthTab),)
    if kind == "fpga.fpga_port":
        return (TabSpec("port", "Port", ("Kind", "Where", "What"), _port_rows, _port_detail,
                        "the scan, the problems the synthesis handed back, what the porter did (src/fpga/, the setup), the RTL orders — questions: o",
                        empty="nothing ported yet — run (r)"),)
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


for _kind in ("fpga.fpga_port", "fpga.sdc", "fpga.fpga_synth", "fpga.timing_util_report", "fpga.ppa_optimize"):
    register_panel(_kind, _builder(_kind))
