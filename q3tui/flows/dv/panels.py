"""TUI panels of the dv flow's steps (the flow's `panels.py`, imported by the TUI when it opens the flow; docs/spec/dv-flow.md).

Read-only tables over what the steps wrote. `testplan`: the tests with the requirements each checks, which test covers each requirement (an open one waits for an
answer on the Questions tab). `tb`: the plain testbench's test cases with the compile result; the UVM testbench's open regions and where the released RTL differs
from the contract. `simulate`: the evidence of the runs. The files, the questions and the log are the usual tabs."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.text import Text

from q3tui.tui.panels import register_panel
from q3tui.tui.views import short
from q3tui.tui.vlsit_panels import TabSpec, VlsitPanel, _tick, artifact

if TYPE_CHECKING:
    from q3tui.pipeline.base import StepDef
    from q3tui.tui.app import Q3TUIApp

CONTRACT = "verification_contract.json"


def _contract(app: "Q3TUIApp") -> dict:
    return artifact(app, CONTRACT, {}) or {}


# -- the tests and the requirements: the verification contract (UVM) or the test plan (plain) ---------------------------------------------------------


PLAN = "test_plan.json"  # the plain testbench's plan
RESULT = "selected_testplan.json"  # the plain testbench's: the plan with the compile result


def _plain(app: "Q3TUIApp") -> bool:
    """The kind of testbench: the plain one has a test plan and no contract (the file the step wrote decides, so the tables follow what is on disk)."""
    return not _contract(app) and bool(artifact(app, PLAN, {}))


def _tests(app: "Q3TUIApp") -> list[dict]:
    """The tests as {tc_id, name, kind, requirement_ids, description, detail}: from the contract, or from the plan."""
    if _plain(app):
        return [{"tc_id": c.get("tc_id", ""), "name": c.get("name", ""), "kind": "–", "requirement_ids": c.get("req_ids") or [], "description": c.get("description", ""),
                 "detail": (f"- only when `{c['conditional_param']}` is enabled" if c.get("conditional_param") else "")}
                for c in (artifact(app, PLAN, {}) or {}).get("test_cases", []) if isinstance(c, dict)]
    return [{**t, "kind": f"{t.get('kind', '')} · {t.get('milestone', '')}", "detail": (f"- objectives: {', '.join(t.get('objective_ids') or []) or '–'}\n- scoreboard rules: "
             f"{', '.join(t.get('rule_ids') or []) or '–'}" + (f"\n- only when `{t['conditional_param']}` is enabled" if t.get("conditional_param") else ""))}
            for t in _contract(app).get("tests", []) if isinstance(t, dict)]


def _test_rows(app: "Q3TUIApp") -> list[tuple]:
    return [(t.get("tc_id", ""), t.get("tc_id", ""), t.get("name", ""), str(t.get("kind", "")), str(len(t.get("requirement_ids") or [])), short(t.get("description", ""), 70))
            for t in _tests(app)]


def _test_detail(app: "Q3TUIApp", key: str) -> str:
    t = next((t for t in _tests(app) if t.get("tc_id") == key), None)
    if t is None:
        return ""
    return f"**{t.get('tc_id')} — {t.get('name')}** ({t.get('kind')})\n\n{t.get('description') or ''}\n\n- checks: {', '.join(t.get('requirement_ids') or []) or '–'}\n{t.get('detail', '')}"


def _spec_reqs(app: "Q3TUIApp") -> list[dict]:
    return [r for r in (artifact(app, "structured_spec.json", {}) or {}).get("requirements", []) if isinstance(r, dict) and r.get("req_id")]


def _requirements(app: "Q3TUIApp") -> list[dict]:
    """The requirements as {req_id, category, status, test_ids, text, detail}: the contract's own, or the spec's with the plan's coverage."""
    if not _plain(app):
        return [{**r, "test_ids": r.get("test_ids") or [],
                 "detail": (f"- status: {r.get('status', '')}{' — ' + r['status_reason'] if r.get('status_reason') else ''}\n- observability: {r.get('observability', '')}\n"
                            f"- tests: {', '.join(r.get('test_ids') or []) or '–'}\n- scoreboard rules: {', '.join(r.get('rule_ids') or []) or '–'}")}
                for r in _contract(app).get("requirements", []) if isinstance(r, dict) and r.get("req_id")]
    from q3tui.flows.dv.plain.plan import required_reqs

    spec = _spec_reqs(app)
    need = set(required_reqs({"requirements": spec}))
    left_out = {e.get("req_id"): e.get("reason", "") for e in ((artifact(app, PLAN, {}) or {}).get("excluded") or []) if isinstance(e, dict)}  # (the planner's: no simulation can check them)
    by: dict[str, list[str]] = {}
    for t in _tests(app):
        for r in t.get("requirement_ids") or []:
            by.setdefault(r, []).append(t["tc_id"])
    out = []
    for r in spec:
        rid = r["req_id"]
        status = "not_observable" if rid not in need else ("planned" if rid in by else "excluded" if rid in left_out else "open")
        why = " — static / assumption: not checked by simulation" if rid not in need else (f" — left out of simulation: {left_out[rid] or 'no reason given'}" if status == "excluded" else "")
        out.append({**r, "status": status, "test_ids": by.get(rid, []), "detail": f"- status: {status}{why}\n- tests: {', '.join(by.get(rid, [])) or '–'}"})
    return out


def _req_status(v) -> Text:
    return Text(str(v), "green" if v == "planned" else "red" if v in ("open", "blocked", "draft") else "grey50")


def _req_rows(app: "Q3TUIApp") -> list[tuple]:
    return [(r["req_id"], r["req_id"], str(r.get("category", "")), _req_status(r.get("status", "")), ", ".join(r.get("test_ids") or []) or "–", short(r.get("text", ""), 70))
            for r in _requirements(app)]


def _req_detail(app: "Q3TUIApp", key: str) -> str:
    r = next((r for r in _requirements(app) if r["req_id"] == key), None)
    if r is None:
        return ""
    return f"**{key}** — {r.get('category', '')}\n\n{r.get('text', '')}\n\n{r.get('detail', '')}"


def _req_urgent(app: "Q3TUIApp") -> int:
    return sum(r.get("status") in ("open", "blocked", "draft") for r in _requirements(app))


# -- the plain testbench: the test cases with the compile result ---------------------------------------------------------------


def _cases(app: "Q3TUIApp") -> list[dict]:
    return [c for c in ((artifact(app, RESULT, {}) or {}).get("test_cases") or []) if isinstance(c, dict)]


def _compile_cell(c: dict) -> Text:
    st = c.get("compile_status")
    if st == "pass":
        return _tick(True)
    if st == "fail":
        return _tick(False)
    return Text(st or "–", "grey50")  # not selected / skipped (no RTL)


def _case_rows(app: "Q3TUIApp") -> list[tuple]:
    return [(c.get("tc_id", ""), c.get("tc_id", ""), c.get("name", ""), str(len(c.get("req_ids") or [])), _tick(c.get("selected")), _compile_cell(c),
             short(c.get("description", ""), 70)) for c in _cases(app)]


def _case_detail(app: "Q3TUIApp", key: str) -> str:
    c = next((c for c in _cases(app) if c.get("tc_id") == key), None)
    if c is None:
        return ""
    return (f"**{c.get('tc_id')} — {c.get('name')}**\n\n{c.get('description') or ''}\n\n- checks: {', '.join(c.get('req_ids') or []) or '–'}\n"
            f"- file: `{c.get('file') or '(not selected: no file)'}`\n- compile: {c.get('compile_status', '–')}")


def _case_hint(app: "Q3TUIApp") -> int:
    return sum(1 for c in _cases(app) if c.get("compile_status") == "fail")


# -- the conformance of the RTL ------------------------------------------------------------------------------------


def _findings(app: "Q3TUIApp") -> list[dict]:
    conf = ((artifact(app, "dut_integration.json", {}) or {}).get("conformance") or {}).get("findings") or []
    val = (artifact(app, "dv_validation.json", {}) or {}).get("findings") or []
    return [{**f, "id": f.get("id") or f"VAL-{n + 1:03d}", "text": f.get("text") or f.get("message", "")} for n, f in enumerate([*conf, *val]) if isinstance(f, dict)]


def _sev(v) -> Text:
    return Text(str(v), "red" if v == "error" else "yellow" if v == "warning" else "grey50")


def _finding_rows(app: "Q3TUIApp") -> list[tuple]:
    return [(f["id"], f["id"], _sev(f.get("severity", "")), str(f.get("code", "")), short(f["text"], 90)) for f in _findings(app)]


def _finding_detail(app: "Q3TUIApp", key: str) -> str:
    f = next((f for f in _findings(app) if f["id"] == key), None)
    return "" if f is None else f"**{key}** — {f.get('severity', '')} {f.get('code', '')}\n\n{f['text']}"


def _finding_urgent(app: "Q3TUIApp") -> int:
    return sum(f.get("severity") == "error" for f in _findings(app))


# -- the open regions of the testbench, the evidence of the runs ----------------------------------------------------------------------


def _items(app: "Q3TUIApp") -> list[dict]:
    return [i for i in (artifact(app, "dv_worklist.json", {}) or {}).get("items", []) if isinstance(i, dict)]


def _item_rows(app: "Q3TUIApp") -> list[tuple]:
    return [(i.get("id", ""), i.get("id", ""), str(i.get("kind", "")), str(i.get("owner", "")), _tick(not i.get("open")), f"{i.get('file', '')}:{i.get('line', '')}")
            for i in _items(app)]


def _item_detail(app: "Q3TUIApp", key: str) -> str:
    i = next((i for i in _items(app) if i.get("id") == key), None)
    return "" if i is None else (f"**{key}** — {i.get('kind', '')}\n\n- file: `{i.get('file', '')}:{i.get('line', '')}`\n- owner: {i.get('owner', '')}\n"
                                  f"- {'open (a DV_TODO region the agent or you implement)' if i.get('open') else 'implemented'}")


def _item_urgent(app: "Q3TUIApp") -> int:
    return int(((artifact(app, "dv_worklist.json", {}) or {}).get("summary") or {}).get("open") or 0)


def _evidence(app: "Q3TUIApp") -> list[dict]:
    return [e for e in (artifact(app, "dv_evidence.json", {}) or {}).get("entries", []) if isinstance(e, dict)]


def _status(v) -> Text:
    return Text(str(v), "green" if v == "PASS" else "red" if v in ("FAIL", "NOT_RUN") else "yellow")


def _evidence_rows(app: "Q3TUIApp") -> list[tuple]:
    return [(e.get("id", ""), e.get("id", ""), str(e.get("kind", "")), str(e.get("name", "")), _status(e.get("status", "")),
             "–" if e.get("uvm_error") is None else f"{e.get('uvm_error')}/{e.get('uvm_fatal')}", str(e.get("log", ""))) for e in _evidence(app)]


def _evidence_detail(app: "Q3TUIApp", key: str) -> str:
    e = next((e for e in _evidence(app) if e.get("id") == key), None)
    return "" if e is None else (f"**{key}** — {e.get('kind', '')} `{e.get('name', '')}`: {e.get('status', '')}\n\n{e.get('note') or ''}\n\n- command: `{e.get('command', '')}`\n"
                                  f"- return code {e.get('returncode')}, {e.get('duration_s')} s, seed {e.get('seed')}\n- log: `{e.get('log', '')}`")


def _evidence_urgent(app: "Q3TUIApp") -> int:
    return sum(e.get("status") not in ("PASS",) for e in _evidence(app))


# -- the triage of the failing tests ---------------------------------------------------------------------------------------------


def _verdicts(app: "Q3TUIApp") -> list[dict]:
    return [v for v in ((artifact(app, "dv_triage.json", {}) or {}).get("verdicts") or []) if isinstance(v, dict)]


_WHERE = {"tb_bug": "→ tb", "rtl_bug": "→ rtl flow", "spec_issue": "→ you (spec)", "unsure": "→ you"}


def _verdict_rows(app: "Q3TUIApp") -> list[tuple]:
    return [(v.get("id", ""), v.get("id", ""), Text(str(v.get("verdict", "")), "red" if v.get("verdict") == "rtl_bug" else "yellow"), _WHERE.get(v.get("verdict", ""), ""),
             str(v.get("module") or "–"), short(v.get("summary", ""), 80)) for v in _verdicts(app)]


def _verdict_detail(app: "Q3TUIApp", key: str) -> str:
    v = next((v for v in _verdicts(app) if v.get("id") == key), None)
    if v is None:
        return ""
    return f"**{key}** — {v.get('verdict')} {_WHERE.get(v.get('verdict', ''), '')}\n\n{v.get('summary', '')}\n\n- request: {v.get('request') or '–'}" + (f"\n- RTL module: {v['module']}" if v.get("module") else "")


def _verdict_urgent(app: "Q3TUIApp") -> int:
    return sum(v.get("verdict") in ("rtl_bug", "spec_issue", "unsure") for v in _verdicts(app))


def _tabs_for(kind: str) -> tuple[TabSpec, ...]:
    if kind == "dv_testplan":
        return (TabSpec("tests", "Tests", ("TC", "Name", "Kind", "REQs", "Description"), _test_rows, _test_detail,
                        "the tests (the verification contract's, or the plain testbench's test plan) and the requirements each checks — approve the step once the open issues are answered",
                        empty="no verification_contract.json / test_plan.json yet — run (r)"),
                TabSpec("reqs", "Requirements", ("REQ", "Category", "Status", "Tests", "Text"), _req_rows, _req_detail,
                        "planned: a test checks it · open / blocked: an issue waits for your answer (the Questions tab) · not_observable / excluded: not simulated, and said so",
                        urgent=_req_urgent, empty="no verification_contract.json / test_plan.json yet — run (r)"))
    if kind == "dv_tb":
        return (TabSpec("cases", "Test cases", ("TC", "Name", "REQs", "Selected", "Compile", "Description"), _case_rows, _case_detail,
                        "plain testbench: each test case and whether it compiles with the RTL — the files are on the Files tab",
                        urgent=_case_hint, empty="no selected_testplan.json — the plain testbench writes it (the UVM one has the Regions and Conformance tabs)"),
                TabSpec("worklist", "Regions", ("Region", "Kind", "Owner", "Done", "Where"), _item_rows, _item_detail,
                        "UVM testbench: the regions the agent (or you) implement — the stages never see the RTL",
                        urgent=_item_urgent, empty="no dv_worklist.json — the UVM testbench writes it"),
                TabSpec("conformance", "Conformance", ("ID", "Severity", "Code", "Finding"), _finding_rows, _finding_detail,
                        "UVM testbench: where the released RTL differs from the frozen contract — reported, never adapted to",
                        urgent=_finding_urgent, empty="no findings — or no dut_integration.json (the UVM testbench's last phase writes it)"))
    if kind == "dv_simulate":
        return (TabSpec("evidence", "Evidence", ("ID", "Kind", "Name", "Status", "UVM err/fatal", "Log"), _evidence_rows, _evidence_detail,
                        "what was really run with the released RTL — a status comes from the simulator log only",
                        urgent=_evidence_urgent, empty="no dv_evidence.json yet — run (r)"),
                TabSpec("triage", "Triage", ("Test", "Verdict", "Goes to", "RTL module", "Summary"), _verdict_rows, _verdict_detail,
                        "whose fault each failing test is: a testbench bug goes back to tb (the chain runs again), an RTL bug is handed to the rtl flow, a spec issue waits for your answer",
                        urgent=_verdict_urgent, empty="no failing test judged yet"))
    return ()


def _builder(kind: str):
    def build(step: "StepDef"):
        return VlsitPanel(step, _tabs_for(kind))

    return build


for _kind in ("dv_testplan", "dv_tb", "dv_simulate"):
    register_panel(_kind, _builder(_kind))
