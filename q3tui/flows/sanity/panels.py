"""TUI panels of the Sanity flow's step kinds (the flow's `panels.py`; docs/spec/sanity-flow.md): the violation table of each lane, the waiver
review's decisions and the check of the waivers. The SDC editor is the FPGA flow's (the same files). Hand-off goes through the flow's action
`sanity_handoff`, as the ASIC flow's panels do."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.text import Text
from textual.binding import Binding

from q3tui.flows.fpga import panels as fpga_panels
from q3tui.flows.sanity.lib.base import check_json, report_json, waivers_json
from q3tui.tui.panels import register_panel
from q3tui.tui.vlsit_panels import DataTab, TabSpec, VlsitPanel, artifact

if TYPE_CHECKING:
    from q3tui.pipeline.base import StepDef
    from q3tui.tui.app import Q3TUIApp

ACTION_STYLE = {"waive": "green", "fix_rtl": "red", "spec": "magenta", "sdc": "yellow", "ask": "yellow"}
ACTION_TITLE = {"waive": "waive", "fix_rtl": "RTL fix", "spec": "spec", "sdc": "SDC", "ask": "ask you"}


def _role(kind: str) -> str:
    return kind.split(".", 1)[1].split("_", 1)[0]  # sanity.cdc_waivers -> cdc


def _decisions(app: "Q3TUIApp", role: str) -> dict[str, dict]:
    return {d["id"]: d for d in (artifact(app, waivers_json(role), {}) or {}).get("items", [])}


def _sent(d: dict | None) -> Text:
    sent = (d or {}).get("handed_off")
    if sent:
        return Text(f"→ {sent['flow']}:{sent['step']}", style="green")
    if d and d["action"] in ("fix_rtl", "spec", "sdc"):
        return Text("not yet", style="yellow")
    return Text("")


# -- violations (cdc / rdc) -----------------------------------------------------------------------------------------------


def _sev(v: str) -> Text:
    return Text(str(v).title(), style="red" if str(v).lower() in ("error", "fatal") else "yellow")


def _violation_rows(role: str):
    def rows(app: "Q3TUIApp") -> list[tuple]:
        by_id = _decisions(app, role)
        out = []
        for v in (artifact(app, report_json(role), {}) or {}).get("violations", []):
            d = by_id.get(v["id"])
            waived = bool(d and d["action"] == "waive")
            out.append((v["id"], v["id"], v["tag"], _sev(v.get("severity", "")), v["message"][:70], Text("Yes", style="green") if waived else Text("No", style="red"),
                        Text(ACTION_TITLE[d["action"]], style=ACTION_STYLE[d["action"]]) if d else Text("not reviewed", style="grey50")))
        return out

    return rows


def _violation_detail(role: str):
    def detail(app: "Q3TUIApp", key: str) -> str:
        v = next((x for x in (artifact(app, report_json(role), {}) or {}).get("violations", []) if x["id"] == key), None)
        if v is None:
            return ""
        fields = "\n".join(f"- **{k}**: `{val}`" for k, val in (v.get("fields") or {}).items() if val)
        d = _decisions(app, role).get(key)
        text = f"**{key}** — `{v['tag']}` ({v.get('severity', '')})\n\n{v['message']}\n\n{fields}"
        if d:
            text += f"\n\n---\n**Review:** {ACTION_TITLE[d['action']]}" + (f" (rule {d['rule']})" if d.get("rule") else "") + f"\n\n{d['reason']}"
        return text

    return detail


# -- the waiver review ---------------------------------------------------------------------------------------------------


def _review_rows(role: str):
    def rows(app: "Q3TUIApp") -> list[tuple]:
        tags = {v["id"]: v["tag"] for v in (artifact(app, report_json(role), {}) or {}).get("violations", [])}
        out = []
        for d in (artifact(app, waivers_json(role), {}) or {}).get("items", []):
            what = d.get("rule") or d.get("module") or ""
            out.append((d["id"], d["id"], tags.get(d["id"], ""), Text(ACTION_TITLE[d["action"]], style=ACTION_STYLE[d["action"]]), what,
                        (d.get("change") or d.get("question") or d.get("reason") or "")[:60],
                        Text(d["answer"][:30], style="cyan") if d.get("answer") else Text(""), _sent(d)))
        return out

    return rows


def _review_detail(role: str):
    def detail(app: "Q3TUIApp", key: str) -> str:
        d = _decisions(app, role).get(key)
        if d is None:
            return ""
        parts = [f"**{key}** — {ACTION_TITLE[d['action']]}" + (f" · rule `{d['rule']}`" if d.get("rule") else "") + (f" · module `{d['module']}`" if d.get("module") else ""),
                 d["reason"]]
        for label, field in (("Change", "change"), ("SDC request", "sdc_request"), ("Question", "question"), ("Your answer", "answer")):
            if d.get(field):
                parts.append(f"**{label}:** " + (f"```tcl\n{d[field]}\n```" if field == "sdc_request" else d[field]))
        if d.get("options"):
            parts.append("**Suggested:** " + "; ".join(d["options"]))
        return "\n\n".join(parts)

    return detail


def _review_urgent(role: str):
    def urgent(app: "Q3TUIApp") -> int:
        return sum(d["action"] in ("fix_rtl", "spec", "sdc") and not d.get("handed_off") for d in _decisions(app, role).values())

    return urgent


def _review_tab(role: str):
    class ReviewTab(DataTab):
        """The decisions: h hands every RTL / spec fix (and the SDC lines that are plain commands) to the flow that owns them; H the highlighted one."""

        BINDINGS = [Binding("h", "handoff", "Hand off all"), Binding("H", "handoff_one", "Hand off this")]

        def _hand(self, ids: str) -> None:
            app = self.capp
            try:
                app.log_info(app.ops.run_action("sanity_handoff", ids=ids))
            except Exception as exc:  # noqa: BLE001
                self.notify_error(exc)
                app.refresh_all()
                return
            app.refresh_all()
            step = next((s for s in app.engine.steps if s.kind == f"sanity.{role}_waivers"), None)
            app.action_switch_flow(str((step.options if step else {}).get("handoff_flow") or "rtl"))

        def action_handoff(self) -> None:
            self._hand("")

        def action_handoff_one(self) -> None:
            key = self.current_key()
            if key:
                self._hand(f"{role}:{key}")

    return ReviewTab


# -- the check of the waivers --------------------------------------------------------------------------------------------


def _check_rows(role: str):
    def rows(app: "Q3TUIApp") -> list[tuple]:
        d = artifact(app, check_json(role), {}) or {}
        if not d:
            return []
        ok = Text("pass", style="green") if d.get("ok") else Text("fail", style="red")
        out = [("result", "Result", "waivers applied, tool run again", "", ok),
               ("total", "Violations found", str(d.get("total", "–")), "", ""),
               ("waived", "Waived", str(d.get("waived", "–")), "", ""),
               ("open", "Open", str(d.get("open", "–")), f"expected {d.get('expected_open', '–')}",
                Text("pass", style="green") if d.get("open") == d.get("expected_open") else Text("fail", style="red"))]
        for key, title in (("still_open_but_waived", "Waived but still reported"), ("waived_but_not_applied", "Open but not expected"), ("not_expressed", "No waive command for")):
            if d.get(key):
                out.append((key, title, ", ".join(d[key]), "", Text("fail", style="red")))
        return out

    return rows


def _check_detail(role: str):
    def detail(app: "Q3TUIApp", key: str) -> str:
        d = artifact(app, check_json(role), {}) or {}
        f = app.project.build_dir / "sanity" / f"{role}_waive.tcl"
        text = f"`{d.get('waivers', f'build/sanity/{role}_waive.tcl')}` — the tool ran again with these waivers ({d.get('runtime_s', '?')} s)"
        if f.is_file():
            text += f"\n\n```tcl\n{f.read_text(errors='replace')[:6000]}\n```"
        return text

    return detail


def _tabs_for(kind: str) -> tuple[TabSpec, ...]:
    if kind == "sanity.sdc":
        return fpga_panels._tabs_for("fpga.sdc")
    role = _role(kind)
    if kind in ("sanity.cdc", "sanity.rdc"):
        return (TabSpec("violations", "Violations", ("No.", "Name", "Severity", "Description", "Waived", "Review"), _violation_rows(role), _violation_detail(role),
                        f"every violation the tool reported; the full table is reports/{role}_report.md / .csv", empty="no violations (or not run yet — r)"),)
    if kind.endswith("_waivers"):
        return (TabSpec("review", "Review", ("No.", "Name", "Decision", "Rule / module", "What", "Your answer", "Handed off"), _review_rows(role), _review_detail(role),
                        "h hand every RTL / spec fix to the flow that owns it (H: the highlighted one) — then run that flow to apply them; questions are in the Questions tab",
                        urgent=_review_urgent(role), widget=_review_tab(role), empty="no review yet — it runs when the analysis found violations (run r)"),)
    if kind.endswith("_check"):
        return (TabSpec("check", "Check", ("Item", "Value", "", "Status"), _check_rows(role), _check_detail(role),
                        "the tool run again with the waivers: the open violations must be exactly the ones not waived", empty="no result yet — run (r)"),)
    return ()


def _builder(kind: str):
    def build(step: "StepDef"):
        return VlsitPanel(step, _tabs_for(kind))

    return build


for _kind in ("sanity.sdc", "sanity.cdc", "sanity.rdc", "sanity.cdc_waivers", "sanity.rdc_waivers", "sanity.cdc_check", "sanity.rdc_check"):
    register_panel(_kind, _builder(_kind))
