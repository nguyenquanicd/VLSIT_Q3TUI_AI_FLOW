"""TUI panels of the ASIC flow's step kinds (the flow's `panels.py`; docs/spec/asic-flow.md): the port findings, the synthesis
numbers and the equivalence result. The SDC editor is the FPGA flow's (the same files). Edits go through the flow's actions."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.text import Text
from textual.binding import Binding

from q3tui.flows.fpga import panels as fpga_panels
from q3tui.tui.panels import register_panel
from q3tui.tui.vlsit_panels import DataTab, TabSpec, VlsitPanel, artifact

if TYPE_CHECKING:
    from q3tui.pipeline.base import StepDef
    from q3tui.tui.app import Q3TUIApp

OK, BAD = Text("pass", style="green"), Text("fail", style="red")


def _status(v: str) -> Text:
    return Text(v, style={"pass": "green", "fail": "red", "warn": "yellow"}.get(v, "grey50"))


# -- ASIC port findings ---------------------------------------------------------------------------------------------


def _port_rows(app: "Q3TUIApp") -> list[tuple]:
    rows = []
    for f in (artifact(app, "asic_port.json", {}) or {}).get("findings", []):
        sent = f.get("handed_off")
        rows.append((f["id"], f["id"], Text(f["severity"], style="red" if f["severity"] == "error" else "yellow"), f["rule"],
                     f"{f['module']}:{f['line']}", f["text"][:70], Text(f"→ {sent['flow']}:{sent['step']}", style="green") if sent else Text("not yet", style="yellow")))
    return rows


def _port_detail(app: "Q3TUIApp", key: str) -> str:
    from q3tui.flows.asic.lib.port import RULES

    f = next((x for x in (artifact(app, "asic_port.json", {}) or {}).get("findings", []) if x["id"] == key), None)
    if f is None:
        return ""
    return f"**{f['id']}** — {RULES[f['rule']].title}\n\n`{f['file']}:{f['line']}` (module `{f['module']}`)\n\n```systemverilog\n{f['text']}\n```\n\n**Fix:** {f['fix']}"


class PortTab(DataTab):
    """The findings: h hands every finding not sent yet (or the highlighted one: H) to the flow that owns the RTL."""

    BINDINGS = [Binding("h", "handoff", "Hand off all"), Binding("H", "handoff_one", "Hand off this")]

    def _hand(self, ids: str) -> None:
        app = self.capp
        try:
            app.log_info(app.ops.run_action("asic_handoff", ids=ids))
        except Exception as exc:  # noqa: BLE001
            self.notify_error(exc)
            app.refresh_all()
            return
        app.refresh_all()
        step = next((s for s in app.engine.steps if s.kind == "asic.asic_port"), None)
        app.action_switch_flow(str((step.options if step else {}).get("handoff_flow") or "vlsit"))

    def action_handoff(self) -> None:
        self._hand("")

    def action_handoff_one(self) -> None:
        key = self.current_key()
        if key:
            self._hand(key)


# -- synthesis numbers --------------------------------------------------------------------------------------------------


def _synth(app: "Q3TUIApp") -> dict:
    return artifact(app, "asic_synth.json", {}) or {}


def _synth_rows(app: "Q3TUIApp") -> list[tuple]:
    d = _synth(app)
    if not d:
        return []
    a, t, p, v, tg = d.get("area", {}), d.get("timing", {}), d.get("power", {}), d.get("verdict", {}), d.get("targets", {})
    rows = [("lib", "Library", ", ".join(x.rsplit("/", 1)[-1] for x in d.get("library", [])), "", ""),
            ("slack", "Worst setup slack (ns)", f"{t.get('slack_ns')}", f"≥ {tg.get('min_slack_ns', 0)}", _status(v.get("timing", "n/a"))),
            ("fmax", "Expected Fmax (MHz)", ("≥ " if all(g.get("bound") == "at_least" for g in t.get("groups", [])) and t.get("groups") else "") + f"{t.get('fmax_mhz', '–')}", "target " + ", ".join(f"{g['target_mhz']:g}" for g in t.get("groups", [])),
             _status("n/a" if not t.get("fmax_mhz") else ("pass" if t["fmax_mhz"] >= max((g["target_mhz"] for g in t.get("groups", [])), default=0) else "fail"))),
            ("tns", "Total negative slack (ns)", f"{t.get('tns_ns', '–')}", "", ""),
            ("path", "Worst path", f"{t.get('startpoint', '')} → {t.get('endpoint', '')}", f"{t.get('logic_levels', '?')} levels", ""),
            ("area", "Area (µm²)", f"{a.get('total_um2')}", f"≤ {tg['max_area_um2']}" if tg.get("max_area_um2") else "", _status(v.get("area", "n/a"))),
            ("cells", "Cells / sequential", f"{a.get('cells')} / {a.get('sequential_cells')}", "", ""),
            ("power", "Power (µW)", f"{p.get('total_uw', '–')}", f"dynamic {p.get('dynamic_uw', '–')}, leakage {p.get('leakage_uw', '–')}", ""),
            ("viol", "Constraint violations", str(d.get("violation_count", 0)), "", _status(v.get("constraints", "n/a")))]
    for k, n in (d.get("check_design") or {}).items():
        rows.append((f"cd_{k}", f"check_design: {k}s", str(n), "", Text("look", style="yellow")))
    for i, w in enumerate(d.get("unconstrained") or []):
        rows.append((f"ct_{i}", "check_timing", w[:80], "", Text("look", style="yellow")))
    return rows


def _synth_detail(app: "Q3TUIApp", key: str) -> str:
    d = _synth(app)
    rep = app.project.root / "asic" / "reports"
    name = {"fmax": "qor.rpt", "slack": "timing.rpt", "path": "timing.rpt", "tns": "qor.rpt", "area": "area.rpt", "cells": "area.rpt", "power": "power.rpt",
            "viol": "constraints.rpt"}.get(key) or ("check_design.rpt" if key.startswith("cd_") else "check_timing.rpt" if key.startswith("ct_") else "")
    f = rep / name if name else None
    if f is not None and f.is_file():
        return f"`asic/reports/{name}`\n\n```\n{f.read_text(errors='replace')[:6000]}\n```"
    return f"Overall: **{d.get('verdict', {}).get('overall', '?')}** — Design Compiler, no placement or routing."


# -- equivalence check ---------------------------------------------------------------------------------------------------


def _lec_rows(app: "Q3TUIApp") -> list[tuple]:
    d = artifact(app, "asic_lec.json", {}) or {}
    if not d:
        return []
    st = d.get("status", "unknown")
    return [("status", "Result", st.replace("_", " "), "", _status("pass" if st == "equivalent" else "fail")),
            ("pass", "Passing compare points", str(d.get("passing", "–")), "", ""), ("fail", "Failing compare points", str(d.get("failing", "–")), "", ""),
            ("nl", "Netlist", d.get("netlist") or "", "", "")]


def _lec_detail(app: "Q3TUIApp", key: str) -> str:
    f = app.project.root / "asic" / "reports" / "lec.rpt"
    return f"```\n{f.read_text(errors='replace')[:6000]}\n```" if f.is_file() else "_no lec.rpt yet_"


def _tabs_for(kind: str) -> tuple[TabSpec, ...]:
    if kind == "asic.sdc":
        return fpga_panels._tabs_for("fpga.sdc")
    if kind == "asic.asic_port":
        return (TabSpec("port", "Findings", ("Id", "Severity", "Rule", "Where", "Code", "Handed off"), _port_rows, _port_detail,
                        "h hand every finding to the flow that owns the RTL (H: the highlighted one) — then run that flow to apply them",
                        widget=PortTab, empty="no findings (or not run yet — r)"),)
    if kind == "asic.asic_synth":
        return (TabSpec("qor", "QoR", ("Item", "Value", "Target", "Status"), _synth_rows, _synth_detail,
                        "set the library in Settings (,) → asic_synth; targets: min_slack_ns, max_area_um2", empty="no asic_synth.json yet — run (r)"),)
    if kind == "asic.asic_lec":
        return (TabSpec("lec", "LEC", ("Item", "Value", "", "Status"), _lec_rows, _lec_detail, "Formality: RTL against the netlist", empty="no result yet — run (r)"),)
    return ()


def _builder(kind: str):
    def build(step: "StepDef"):
        return VlsitPanel(step, _tabs_for(kind))

    return build


for _kind in ("asic.sdc", "asic.asic_port", "asic.asic_synth", "asic.asic_lec"):
    register_panel(_kind, _builder(_kind))
