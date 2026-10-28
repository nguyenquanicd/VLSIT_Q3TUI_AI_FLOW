"""The dv flow's TUI panels (flows/dv/panels.py): the testplan, tb and simulate steps' tables over the files they wrote."""

from __future__ import annotations

import json

import pytest
from textual.widgets import DataTable

from q3tui.core.project import Project
from q3tui.tui.app import Q3TUIApp
from q3tui.tui.vlsit_panels import DataTab


@pytest.fixture
def proj(tmp_path):
    (tmp_path / "q3tui.yaml").write_text("pipeline: {flow: dv}\n")
    (tmp_path / "schemas").mkdir()
    contract = {"requirements": [{"req_id": "REQ-001", "category": "functional", "text": "reset clears the counter", "status": "planned", "test_ids": ["TC-001"]},
                                 {"req_id": "REQ-002", "category": "functional", "text": "count up on enable", "status": "open", "test_ids": []},
                                 {"req_id": "REQ-003", "category": "structural", "text": "ports are fixed", "status": "not_observable", "test_ids": []}],
                "tests": [{"tc_id": "TC-001", "name": "reset", "description": "reset clears it", "kind": "smoke", "milestone": "v0_1", "requirement_ids": ["REQ-001"]},
                          {"tc_id": "TC-002", "name": "count", "description": "counts", "kind": "directed", "milestone": "later", "requirement_ids": [], "conditional_param": "PR_EN"}]}
    integ = {"conformance": {"findings": [{"id": "CONF-001", "severity": "error", "code": "port_missing", "text": "o_done is not in the RTL"}]}}
    worklist = {"summary": {"open": 1}, "items": [{"id": "SBR-001", "kind": "scoreboard", "file": "scoreboard/dut_scoreboard.svh", "line": 12, "owner": "agent", "open": True},
                                                  {"id": "seq.smoke", "kind": "sequence", "file": "sequences/s.svh", "line": 3, "owner": "agent", "open": False}]}
    evidence = {"entries": [{"id": "run-001", "kind": "run", "name": "dut_smoke_test", "status": "PASS", "uvm_error": 0, "uvm_fatal": 0, "log": "build/sim/r1.log"},
                            {"id": "run-002", "kind": "run", "name": "dut_tc_002_test", "status": "FAIL", "uvm_error": 2, "uvm_fatal": 0, "log": "build/sim/r2.log", "note": "UVM_ERROR=2"}]}
    for name, data in (("verification_contract.json", contract), ("dut_integration.json", integ), ("dv_worklist.json", worklist),
                       ("dv_evidence.json", evidence)):
        (tmp_path / "schemas" / name).write_text(json.dumps(data))
    return Project.open(tmp_path)


def _rows(tab) -> dict[str, list[str]]:
    t = tab.query_one(DataTable)
    return {t.coordinate_to_cell_key((i, 0)).row_key.value: [str(t.get_cell_at((i, c))) for c in range(len(t.columns))] for i in range(t.row_count)}


def _tabs(app, step: str) -> dict:
    app._select_step(step)
    panel = app._panel(step)
    tabs = {w.tab.id: w for w in panel.query(DataTab)}
    for w in tabs.values():
        w.refresh_view()
    return tabs


async def test_the_testplan_step_has_the_tests_and_the_requirements(proj):
    from q3tui.flows.dv import panels

    app = Q3TUIApp(proj)
    async with app.run_test(size=(170, 50)) as pilot:
        await pilot.pause()
        tabs = _tabs(app, "testplan")
        await pilot.pause()
        assert set(tabs) == {"tests", "reqs"}  # (the open issues are the standard Questions tab)
        tests = _rows(tabs["tests"])
        assert tests["TC-001"][:4] == ["TC-001", "reset", "smoke · v0_1", "1"] and tests["TC-002"][3] == "0"
        reqs = _rows(tabs["reqs"])
        assert reqs["REQ-001"][2:4] == ["planned", "TC-001"] and reqs["REQ-002"][2:4] == ["open", "–"] and reqs["REQ-003"][2] == "not_observable"
        assert panels._req_urgent(app) == 1  # one requirement waits for an answer


async def test_the_tb_step_has_the_open_regions_and_the_conformance(proj):
    from q3tui.flows.dv import panels

    app = Q3TUIApp(proj)
    async with app.run_test(size=(170, 50)) as pilot:
        await pilot.pause()
        tabs = _tabs(app, "tb")
        await pilot.pause()
        assert set(tabs) == {"cases", "worklist", "conformance"}  # (cases: the plain testbench's; empty here)
        assert _rows(tabs["worklist"])["SBR-001"][2] == "agent" and _rows(tabs["conformance"])["CONF-001"][2] == "port_missing"
        assert panels._item_urgent(app) == 1 and panels._finding_urgent(app) == 1  # one region still open, one finding with severity error


async def test_the_simulate_step_has_the_evidence(proj):
    from q3tui.flows.dv import panels

    app = Q3TUIApp(proj)
    async with app.run_test(size=(170, 50)) as pilot:
        await pilot.pause()
        tabs = _tabs(app, "simulate")
        await pilot.pause()
        assert set(tabs) == {"tests", "evidence", "triage"}
        ev = _rows(tabs["evidence"])
        assert ev["run-001"][3] == "PASS" and ev["run-002"][3] == "FAIL" and ev["run-002"][4] == "2/0"
        assert panels._evidence_urgent(app) == 1  # a run that failed


async def test_the_tables_are_empty_before_the_steps_ran(tmp_path):
    (tmp_path / "q3tui.yaml").write_text("pipeline: {flow: dv}\n")
    app = Q3TUIApp(Project.open(tmp_path))
    async with app.run_test(size=(170, 50)) as pilot:
        await pilot.pause()
        for step in ("testplan", "tb", "simulate"):
            for tab in _tabs(app, step).values():
                assert tab.query_one(DataTable).row_count == 0


# -- the plain testbench: the same tabs read the test plan and the plan with its compile result -----------------------------------------------------------

@pytest.fixture
def plain(tmp_path):
    (tmp_path / "q3tui.yaml").write_text("pipeline: {flow: dv}\n")
    (tmp_path / "schemas").mkdir()
    spec = {"requirements": [{"req_id": "REQ-001", "category": "functional", "text": "reset clears the counter"},
                             {"req_id": "REQ-002", "category": "functional", "text": "count up on enable"},
                             {"req_id": "REQ-003", "category": "structural", "text": "ports are fixed", "sva_hint": "static"},
                             {"req_id": "REQ-004", "category": "coding", "text": "one module per file"}]}
    planned = {"test_cases": [{"tc_id": "TC-001", "name": "reset", "description": "reset clears it", "req_ids": ["REQ-001"], "selected": True},
                              {"tc_id": "TC-002", "name": "count", "description": "counts", "req_ids": [], "selected": True, "conditional_param": "PR_EN"}],
               "excluded": [{"req_id": "REQ-004", "reason": "a lint rule"}]}
    result = {"test_cases": [{"tc_id": "TC-001", "name": "reset", "description": "reset clears it", "req_ids": ["REQ-001"], "selected": True,
                              "file": "src/tb/tests/tc_001_reset.sv", "compile_status": "pass"},
                             {"tc_id": "TC-002", "name": "count", "description": "counts", "req_ids": [], "selected": True, "compile_status": "fail"}]}
    for name, data in (("structured_spec.json", spec), ("test_plan.json", planned), ("selected_testplan.json", result)):
        (tmp_path / "schemas" / name).write_text(json.dumps(data))
    return Project.open(tmp_path)


async def test_the_plain_testbench_has_its_plan_its_coverage_and_its_compile_result(plain):
    from q3tui.flows.dv import panels

    app = Q3TUIApp(plain)
    async with app.run_test(size=(170, 50)) as pilot:
        await pilot.pause()
        plan = _tabs(app, "testplan")
        await pilot.pause()
        tests = _rows(plan["tests"])
        assert tests["TC-001"][:4] == ["TC-001", "reset", "–", "1"] and tests["TC-002"][3] == "0"
        reqs = _rows(plan["reqs"])
        assert reqs["REQ-001"][2:4] == ["planned", "TC-001"] and reqs["REQ-002"][2:4] == ["open", "–"] and reqs["REQ-003"][2] == "not_observable"  # (static: not simulated)
        assert reqs["REQ-004"][2] == "excluded" and "a lint rule" in panels._req_detail(app, "REQ-004")  # (the planner's: no simulation can check it, and why)
        assert panels._req_urgent(app) == 1  # one requirement a simulation can check has no test case
        tb = _tabs(app, "tb")
        await pilot.pause()
        cases = _rows(tb["cases"])
        assert cases["TC-001"][:3] == ["TC-001", "reset", "1"] and cases["TC-001"][4] != cases["TC-002"][4]  # (passes vs fails to compile)
        assert panels._case_hint(app) == 1


async def test_the_simulate_step_lists_every_test_with_its_status_while_the_tests_run(plain):
    import os
    import time

    from q3tui.flows.dv import panels

    (plain.root / "schemas" / "selected_testplan.json").write_text(json.dumps({"test_cases": [
        {"tc_id": f"TC-00{i}", "name": f"t{i}", "req_ids": [], "selected": True} for i in range(1, 5)]}))
    live = {"pid": os.getpid(), "ended": None, "jobs": 1, "order": ["TC-001", "TC-002", "TC-003"], "tests": [
        {"tc_id": "TC-001", "name": "t1", "status": "PASS", "duration_s": 75.0, "last": "FAIL", "log": "build/sim/tb/run_TC-001.log", "est_s": 70.0},
        {"tc_id": "TC-002", "name": "t2", "status": "RUNNING", "t0": time.time() - 30, "last": "PASS", "est_s": 90.0},
        {"tc_id": "TC-003", "name": "t3", "status": "PENDING", "last": None, "est_s": 120.0}]}
    (plain.root / "schemas" / "dv_test_results.json").write_text(json.dumps(live))
    app = Q3TUIApp(plain)
    async with app.run_test(size=(170, 50)) as pilot:
        await pilot.pause()
        tabs = _tabs(app, "simulate")
        await pilot.pause()
        assert list(tabs)[0] == "tests"
        rows = _rows(tabs["tests"])
        assert rows["TC-001"][2:6] == ["PASS", "1:15", "1:10", "FAIL"] and rows["TC-002"][2] == "RUNNING" and rows["TC-002"][3].startswith("0:3")
        assert rows["TC-002"][4] == "1:30" and rows["TC-003"][2] == "PENDING" and rows["TC-004"][2] == "–"     # (TC-004: in the plan, not in this run)
        # time left: TC-002 has 1 min to go, then TC-003 2 min, on the one slot
        assert panels._run_summary(app) == "4 tests: 1 PASS · 1 RUNNING · 1 PENDING · 1 not run yet · ≈ 3 min left" and panels._run_urgent(app) == 0
        assert "run_TC-001.log" in panels._run_detail(app, "TC-001") and "estimate: 1:30" in panels._run_detail(app, "TC-002")
    (plain.root / "schemas" / "dv_test_results.json").write_text(json.dumps({**live, "pid": 2 ** 22 + 12345}))  # (the run's process is gone: killed)
    app = Q3TUIApp(plain)
    async with app.run_test(size=(170, 50)) as pilot:
        await pilot.pause()
        rows = _rows(_tabs(app, "simulate")["tests"])
        await pilot.pause()
        assert rows["TC-002"][2] == "NOT_RUN" and rows["TC-003"][2] == "NOT_RUN" and panels._run_urgent(app) == 2


async def test_a_panel_that_cannot_be_built_gets_the_plain_one_and_the_tui_stays_up(plain, monkeypatch):
    from q3tui.tui import vlsit_panels
    from q3tui.tui.panels import DefaultPanel

    real = vlsit_panels.TabSpec

    def old_tabspec(*a, summary=None, **kw):  # (a TUI session that imported vlsit_panels before TabSpec had `summary`, reading a newer panels.py)
        if summary is not None:
            raise TypeError("TabSpec.__init__() got an unexpected keyword argument 'summary'")
        return real(*a, **kw)

    monkeypatch.setattr(vlsit_panels, "TabSpec", old_tabspec)
    app = Q3TUIApp(plain)
    said = []
    monkeypatch.setattr(app, "notify", lambda msg, **kw: said.append(msg))
    async with app.run_test(size=(170, 50)) as pilot:
        await pilot.pause()
        assert type(app._panel("simulate")) is DefaultPanel and type(app._panel("tb")) is not DefaultPanel
        assert len(said) == 1 and "simulate: TypeError" in said[0] and "restart the TUI" in said[0]


async def test_the_simulate_step_shows_the_verdicts(plain):
    from q3tui.flows.dv import panels

    (plain.root / "schemas" / "dv_triage.json").write_text(json.dumps({"round": 1, "verdicts": [
        {"id": "TC-019", "verdict": "tb_bug", "summary": "the check says X while printing 0", "request": "fix the check"},
        {"id": "TC-020", "verdict": "rtl_bug", "module": "m_core", "summary": "o_done is late", "request": "assert o_done in the same cycle"}]}))
    app = Q3TUIApp(plain)
    async with app.run_test(size=(170, 50)) as pilot:
        await pilot.pause()
        tabs = _tabs(app, "simulate")
        await pilot.pause()
        assert set(tabs) == {"tests", "evidence", "triage"}
        rows = _rows(tabs["triage"])
        assert rows["TC-019"][1:3] == ["tb_bug", "→ tb"] and rows["TC-020"][1:4] == ["rtl_bug", "→ rtl flow", "m_core"]
        assert panels._verdict_urgent(app) == 1  # the RTL bug waits for the rtl flow
