"""The fix loop (core/loop.py, `q3tui loop`): analysis → hand-off → owner flow → analysis, with the regression check, on the fake VC Static
and the fake review of tests/test_sanity.py. The owner flow's `rtl` step is a code-only step that swaps the fake tool's report, standing for
an RTL update that fixes (or does not fix) the violations."""

import json

import anyio
import pytest
from click.testing import CliRunner

from q3tui.core import loop
from q3tui.core.cli import main
from q3tui.core.events import EventBus
from q3tui.core.project import Project
from q3tui.llm import runtime
from q3tui.steps import register_kind
from q3tui.pipeline.base import StepDef
from tests.test_sanity import CDC, ReviewLLM, _decide, _tools, proj  # noqa: F401  (fixtures)


class FixerStep(StepDef):
    """Code-only step of the owner flow: each run of `rtl` applies the next fake tool report (what the updated RTL would show)."""

    title = "Fixer"
    reports: list = []

    def inputs(self, engine):
        return []

    def outputs(self, engine):
        p = engine.project.root / "notes" / f"{self.name}.txt"
        return [p] if p.is_file() else []

    async def run(self, ctx):
        (ctx.project.root / "notes").mkdir(exist_ok=True)
        (ctx.project.root / "notes" / f"{self.name}.txt").write_text("ran\n")
        if self.name == "rtl" and FixerStep.reports:
            rtl = ctx.project.rtl_dir / "blk.sv"
            rtl.write_text(rtl.read_text() + f"// update {len(FixerStep.reports)}\n")  # (the analysis runs again only on changed RTL)
            (ctx.project.root / "cdc.json").write_text(json.dumps(FixerStep.reports.pop(0)))


register_kind("fixer", "tests.test_loop:FixerStep")
OWNER = "name: owner\nsteps:\n  - {id: spec, kind: fixer}\n  - {id: rtl, kind: fixer, deps: [spec]}\n"


@pytest.fixture
def loop_proj(proj, monkeypatch):
    (proj.root / "flows").mkdir(exist_ok=True)
    (proj.root / "flows" / "owner.yaml").write_text(OWNER)
    _tools(proj.root, rdc=[], extra="  gate_modes: {cdc_waivers: auto}\n  step_options:\n    cdc_waivers:\n      handoff_flow: owner\n    rdc_waivers:\n      handoff_flow: owner\n")
    monkeypatch.setattr(runtime, "run_stage", ReviewLLM(_decide))
    FixerStep.reports = []
    return Project.open(proj.root)


def run_loop(project, **kw):
    return anyio.run(lambda: loop.fix_loop(project, EventBus(), "sanity", **kw))


def test_a_round_that_fixes_everything_ends_the_loop_clean(loop_proj):
    FixerStep.reports = [[CDC[0]]]  # the updated RTL: only the first crossing is left, and the review waives that one
    res = run_loop(loop_proj)
    assert res.stopped == "clean" and res.ok
    assert [(r.number, r.open) for r in res.rounds] == [(0, 2), (1, 0)] and res.best == 1
    assert "cdc:V2 → owner:rtl" in res.rounds[1].note and res.rounds[1].cost_usd >= 0
    assert (loop.best_dir(loop_proj) / "blk.sv").is_file() and "round" in loop.summary(res)


def test_a_round_that_does_not_improve_stops_and_the_best_rtl_is_kept(loop_proj):
    FixerStep.reports = [CDC, CDC]  # the "update" fixed nothing
    original = (loop_proj.rtl_dir / "blk.sv").read_text()
    res = run_loop(loop_proj)
    assert res.stopped == "no improvement" and res.ok and res.best == 0
    assert [(r.number, r.open) for r in res.rounds] == [(0, 2), (1, 2)] and "not better than round 0" in res.rounds[1].note
    rtl = loop_proj.rtl_dir / "blk.sv"
    assert "// update" in rtl.read_text()  # (the owner flow changed the RTL in round 1)
    assert "restored the RTL of round 0" in loop.restore_best(loop_proj) and rtl.read_text() == original


def test_the_loop_stops_at_the_spend_limit_before_a_new_round(loop_proj):
    res = run_loop(loop_proj, max_cost_usd=0.0)
    assert res.stopped == "spend limit" and [r.number for r in res.rounds] == [0] and not res.ok


def test_nothing_to_hand_off_ends_the_loop(loop_proj):
    (loop_proj.root / "cdc.json").write_text(json.dumps([CDC[2]]))  # only the SDC request: the SDC is hand-written, it stays yours
    res = run_loop(loop_proj)
    assert res.stopped == "nothing to hand off" and res.rounds[0].open == 1


def test_the_cli_command_runs_the_loop(loop_proj):
    FixerStep.reports = [[CDC[0]]]
    r = CliRunner().invoke(main, ["-C", str(loop_proj.root), "loop"])
    assert r.exit_code == 0, r.output
    assert "stopped: clean" in r.output and "best round 1" in r.output
    assert "restored the RTL of round 1" in CliRunner().invoke(main, ["-C", str(loop_proj.root), "loop", "--restore-best"]).output
