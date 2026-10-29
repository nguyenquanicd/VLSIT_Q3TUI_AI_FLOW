"""Runs of several flows in one project folder (docs/spec/pipeline.md "Runs of several flows"): `r` in dv, then fpga, then sanity. One run per
flow, one state file merged per step, a step two runs share runs once, an input another run changes keeps a step stale, and a checking run
that would change the design goes on in a copy (core/lanes.py `fork_run`). The step kinds and the project are the lanes tests'."""

from __future__ import annotations

import json
import subprocess
import sys

import anyio
import pytest

from q3tui import flows
from q3tui.core import lanes
from q3tui.pipeline.base import StepContext, StepDef
from q3tui.pipeline.engine import Engine
from tests.test_lanes import _flow as _lane_flow
from tests.test_lanes import _kinds, _project  # noqa: F401 - (_kinds: the step kinds, an autouse fixture)


class TDirect(StepDef):
    """As the FPGA PPA step: unattended, it hands a fix to the design flow and runs that flow's rtl step itself (a flow run inside the checking
    run, behind an `except Exception`)."""

    title = "T direct"

    def inputs(self, engine):
        return [engine.project.root / "src" / "rtl" / "top.sv"]

    def outputs(self, engine):
        p = engine.project.root / "results" / "direct.txt"
        return [p] if p.is_file() else []

    async def run(self, ctx: StepContext) -> None:
        (ctx.project.root / "results").mkdir(exist_ok=True)
        (ctx.project.root / "results" / "direct.txt").write_text((ctx.project.root / "src" / "rtl" / "top.sv").read_text())

    async def unattended(self, engine) -> bool:
        if "// fix: D" in (engine.project.root / "src" / "rtl" / "top.sv").read_text():
            return False
        Engine(engine.project, engine.bus, flow=flows.load_flow("design", engine.project.root)).request_change("rtl", "[rtl:top] D")
        try:
            await Engine(engine.project, engine.bus, flow=flows.load_flow("design", engine.project.root)).run(only="rtl", yes=True)
        except Exception:  # noqa: BLE001 - (as the PPA step: a hand-off that failed is said, not raised)
            return False
        return True


@pytest.fixture(autouse=True)
def _direct_kind():
    from q3tui.steps import KINDS, register_kind

    register_kind("t_direct", "tests.test_parallel_runs:TDirect")
    yield
    KINDS.pop("t_direct", None)


@pytest.fixture
def other_run():
    """A live process standing in for a run of another flow: `lock(flow)` writes its lock."""
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])

    def lock(project, flow: str, phase: str = "running") -> int:
        project.ensure_state_dir()
        (project.state_dir / f"run.{flow}.lock").write_text(json.dumps({"pid": p.pid, "started": "2026-01-01T00:00:00", "flow": flow, "phase": phase}))
        return p.pid

    lock.process = p
    yield lock
    p.kill()
    p.wait()


def _flow(project, name):
    return flows.load_flow(name, project.root)


def test_the_runs_of_two_flows_share_the_state_file(tmp_path, other_run):
    project = _project(tmp_path, lane_flows=("lb", "lc"))
    cost0 = Engine(project).state.total_cost_usd
    a, b = Engine(project, flow=_flow(project, "lb")), Engine(project, flow=_flow(project, "lc"))
    a.state.step_meta["check_b"] = {"x": 1}
    a.state.feedback.setdefault("rtl", []).append("[rtl:top] from lb")
    a.state.total_cost_usd += 1.0
    b.state.step_meta["check_c"] = {"y": 2}
    b.state.feedback.setdefault("rtl", []).append("[rtl:top] from lc")
    b.state.total_cost_usd += 2.0
    a.save()
    b.save()  # (b never read a's changes: per step, each keeps what it changed)
    st = Engine(project).state
    assert st.step_meta["check_b"] == {"x": 1} and st.step_meta["check_c"] == {"y": 2}
    assert st.feedback["rtl"] == ["[rtl:top] from lb", "[rtl:top] from lc"] and st.total_cost_usd == pytest.approx(cost0 + 3.0)
    a.state.feedback["rtl"].remove("[rtl:top] from lb")  # (a consumed its own request: b's stays)
    a.save()
    assert Engine(project).state.feedback["rtl"] == ["[rtl:top] from lc"]
    # one run per flow: a run of lb elsewhere — a TUI on lb only watches it, lc saves and runs beside it
    pid = other_run(project, "lb")
    a2, b2 = Engine(project, flow=_flow(project, "lb")), Engine(project, flow=_flow(project, "lc"))
    assert a2.other_run()["pid"] == pid and b2.other_run() is None and [r["flow"] for r in b2.other_runs()] == ["lb"]
    a2.state.step_meta["check_b"] = {"x": 9}
    a2.save()
    b2.state.step_meta["check_c"] = {"y": 3}
    b2.save()
    st = Engine(project).state
    assert st.step_meta["check_b"] == {"x": 1} and st.step_meta["check_c"] == {"y": 3}
    assert anyio.run(lambda: a2.run(yes=True)) == "blocked"


def test_a_step_another_run_executes_is_waited_for(tmp_path, other_run, monkeypatch):
    monkeypatch.setattr(Engine, "WAIT_S", 0.05)
    project = _project(tmp_path, lane_flows=("lc",))
    (project.root / "spec" / "intent.md").write_text("a block, v2\n")  # (spec is out of date for every flow that has it)
    eng0 = Engine(project, flow=_flow(project, "design"))
    rec = eng0.state.step("spec")
    rec.status, rec.pid = "running", other_run.process.pid  # another process runs it
    eng0.save()
    seen = []
    eng = Engine(project, flow=_flow(project, "lc"))
    eng.bus.subscribe(seen.append)
    out = {}

    async def main():
        async def run():
            out["outcome"] = await eng.run(yes=True)

        async with anyio.create_task_group() as tg:
            tg.start_soon(run)
            await anyio.sleep(0.5)
            out["waited"] = any("spec is running in another run: waiting" in str(e.data.get("message")) for e in seen)
            out["spec_then"] = Engine(project).state.steps["spec"].pid
            other_run.process.kill()  # (that run ends; here it was killed: the step is interrupted, this run runs it itself)

    anyio.run(main)
    assert out["waited"] and out["spec_then"] == other_run.process.pid and out["outcome"] == "complete"
    st = Engine(project).state.steps["spec"]
    assert st.status == "done" and st.pid is None


def test_an_input_another_run_changes_while_a_step_runs_keeps_it_stale(tmp_path, other_run):
    project = _project(tmp_path, lane_flows=("lc",))
    other_run(project, "lb")  # (another flow's run goes on meanwhile)
    eng = Engine(project, flow=_flow(project, "lc"))
    seen = []
    eng.bus.subscribe(seen.append)

    async def main():
        async def edit():
            await anyio.sleep(0.15)  # (check_c sleeps 0.3: its RTL changes under it)
            (project.root / "src" / "rtl" / "top.sv").write_text("module top;\n// fix: elsewhere\nendmodule\n")

        async with anyio.create_task_group() as tg:
            tg.start_soon(edit)
            assert await eng.run(yes=True) == "complete"

    anyio.run(main)
    assert any("src/rtl/top.sv changed in another run while check_c ran" in str(e.data.get("message")) for e in seen)
    views = {v.name: v.status for v in Engine(project, flow=_flow(project, "lc")).status()}
    assert views["check_c"] == "stale"


def test_a_checking_run_that_would_change_the_design_goes_on_in_a_copy(tmp_path):
    project = _project(tmp_path, lane_flows=("lb",))
    project.cfg.lanes.auto_merge = False
    eng = Engine(project, flow=_flow(project, "lb"))
    seen = []
    eng.bus.subscribe(seen.append)
    assert anyio.run(lambda: eng.run(yes=True)) == "complete"
    assert (project.root / "src" / "rtl" / "top.sv").read_text() == "module top;\nendmodule\n"  # your folder: the design as it was
    assert lanes.latest_tag(project) == "v1" and json.loads((lanes.tags_dir(project) / "v1" / "tag.json").read_text())["fork"] == "lb"
    lane = lanes.lane_dir(project, "v1", "lb")
    assert "// fix: B" in (lane / "src" / "rtl" / "top.sv").read_text()                   # the fix, in the copy
    assert [(c["kind"], c["target"], c["origin"]) for c in lanes.read_changes(lane)] == [("request", "rtl", "lb (forked at rtl)")]
    assert lanes.tag_results(project, "v1")["lb"]["status"] == "fixed" and not Engine(project).state.feedback.get("rtl")
    msgs = [str(e.data.get("message")) for e in seen]
    assert any("lb: the fix changes the design (rtl): going on in its own copy" in m for m in msgs)
    assert any("its fixes wait in the copy: merge them with m" in m for m in msgs)
    m = anyio.run(lambda: lanes.merge(project, "v1", ["lb"]))                                # (m on the Lanes screen)
    assert m["merged"] and "// fix: B" in (project.root / "src" / "rtl" / "top.sv").read_text()


def test_a_fork_merges_by_itself_once_the_other_runs_have_ended(tmp_path, other_run, monkeypatch):
    monkeypatch.setattr(Engine, "WAIT_S", 0.05)
    project = _project(tmp_path, lane_flows=("lb", "lc"))
    other_run(project, "lc")
    eng = Engine(project, flow=_flow(project, "lb"))
    seen = []
    eng.bus.subscribe(seen.append)
    out = {}

    async def main():
        async def run():
            out["outcome"] = await eng.run(yes=True)

        async with anyio.create_task_group() as tg:
            tg.start_soon(run)
            for _ in range(200):
                await anyio.sleep(0.05)
                if any("merging when the run(s) of lc end" in str(e.data.get("message")) for e in seen):
                    break
            out["top_meanwhile"] = (project.root / "src" / "rtl" / "top.sv").read_text()
            other_run.process.kill()  # (the lc run ends)

    anyio.run(main)
    assert out["top_meanwhile"] == "module top;\nendmodule\n" and out["outcome"] == "complete"
    assert "// fix: B" in (project.root / "src" / "rtl" / "top.sv").read_text() and lanes.latest_tag(project) == "v2"
    assert not (project.state_dir / "run.lb.lock").exists()


def test_a_flow_run_inside_the_checking_run_forks_the_checking_run(tmp_path):
    project = _project(tmp_path, lane_flows=("ld",))
    _lane_flow(project.root, "ld", "- {id: spec, kind: t_spec}\n- {id: direct, kind: t_direct, deps: [spec]}")
    project.cfg.pipeline.auto_answer, project.cfg.lanes.auto_merge = True, False
    eng = Engine(project, flow=_flow(project, "ld"))
    seen = []
    eng.bus.subscribe(seen.append)
    assert anyio.run(lambda: eng.run(yes=True)) == "complete"
    assert (project.root / "src" / "rtl" / "top.sv").read_text() == "module top;\nendmodule\n"
    lane = lanes.lane_dir(project, "v1", "ld")
    assert "// fix: D" in (lane / "src" / "rtl" / "top.sv").read_text() and lanes.tag_results(project, "v1")["ld"]["status"] == "fixed"
    assert not [e for e in seen if e.kind == "run_finished" and e.data.get("outcome") == "cancelled"]   # (the nested run is not "cancelled")
    assert not (project.state_dir / "run.design.lock").exists() and not (project.state_dir / "run.ld.lock").exists()
