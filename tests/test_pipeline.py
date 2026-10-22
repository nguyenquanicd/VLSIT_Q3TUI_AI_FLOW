import anyio
import pytest

from q3tui.llm import runtime
from q3tui.pipeline.engine import Engine, EngineError
from q3tui.core.project import Project
from tests.fakes import SPEC, FakeLLM, use_test_flow


@pytest.fixture
def llm(monkeypatch):
    fake = FakeLLM()
    monkeypatch.setattr(runtime, "run_stage", fake)
    return fake


@pytest.fixture
def engine(tmp_path):
    use_test_flow(tmp_path)  # the spec step, then two code-only steps that read it: the engine's own behaviour
    return Engine(Project.open(tmp_path))


def status(engine):
    return {v.name: (v.status, v.gate) for v in engine.status()}


def run(engine, **kw):
    return anyio.run(lambda: engine.run(**kw))


def blocking_spec_question(engine, llm):
    engine.project.cfg.spec.self_review = False  # (the fake review re-asks the default, non-blocking question)
    llm.spec_outputs.append({**SPEC.model_dump(), "questions": [
        {"id": "", "question": "Should drop_count saturate?", "blocking": True, "default_assumption": "saturate"}]})


def test_empty_project_is_blocked(engine, llm):
    assert status(engine)["spec"] == ("missing_input", "none")
    assert run(engine) == "blocked"
    assert llm.calls == []


def test_events_logged(engine, llm):
    engine.import_intent("x")
    kinds = []
    engine.bus.subscribe(lambda e: kinds.append(e.kind))
    run(engine)
    assert kinds[0] == "run_started" and "gate_opened" in kinds and kinds[-1] == "run_finished"
    logs = list((engine.project.state_dir / "runs").glob("*/events.jsonl"))
    assert logs and "step_started" in logs[0].read_text()


def test_project_root_ignores_user_config_dir(tmp_path, monkeypatch):
    from q3tui.core.project import find_project_root

    home = tmp_path / "home"
    (home / ".q3tui").mkdir(parents=True)
    (home / ".q3tui" / "q3tui.yaml").write_text("{}\n")
    (home / ".q3tui" / "pipeline.json").write_text("{}")  # even a stray state file there is ignored
    monkeypatch.setenv("Q3TUI_HOME", str(home / ".q3tui"))
    proj = home / "work" / "blk"
    (proj / "spec").mkdir(parents=True)
    assert find_project_root(proj) == proj
    assert find_project_root(proj / "spec") == proj / "spec"  # no marker yet: cwd is the project

    (proj / ".q3tui").mkdir()
    (proj / ".q3tui" / "pipeline.json").write_text("{}")
    assert find_project_root(proj / "spec") == proj


async def test_quit_confirms_while_running(tmp_path, llm):
    from q3tui.tui.app import Q3TUIApp
    from q3tui.tui.screens import ConfirmScreen

    (tmp_path / "q3tui.yaml").write_text("{}\n")
    app = Q3TUIApp(Project.open(tmp_path))
    async with app.run_test() as pilot:
        await app.action_quit()  # idle → exits immediately
        await pilot.pause()
    assert app.return_code is not None or not app.is_running

    app2 = Q3TUIApp(Project.open(tmp_path))
    async with app2.run_test() as pilot:
        release = anyio.Event()

        async def busy():
            await release.wait()

        app2._pipeline_worker = app2.run_worker(busy(), group="pipeline")
        app2.engine.running = "req"
        await pilot.pause()
        await app2.action_quit()
        await pilot.pause()
        assert isinstance(app2.screen, ConfirmScreen)
        await pilot.press("n")
        await pilot.pause()
        assert app2.is_running
        release.set()


def test_cost_reset(engine, llm):
    engine.import_intent("x")
    run(engine, yes=True)
    assert engine.cost_summary()["total_usd"] > 0
    msg = engine.reset_cost()
    assert "reset to $0.00" in msg
    engine.reload()
    assert engine.state.total_cost_usd == 0 and engine.state.cost_resets[0]["amount_usd"] > 0
    assert engine.cost_summary()["steps"] == {}


def test_orphaned_generated_spec_is_not_treated_as_user_file(engine, llm):
    engine.import_intent("x")
    run(engine, yes=True)
    engine.state.steps.pop("spec")  # e.g. state lost / interrupted
    engine.save()
    assert status(engine)["spec"][0] == "pending"  # not "user": spec.md has Q3TUI section markers
    names = [f.name for f in engine.reset_plan("spec", only=True)["spec"]]
    assert "spec.md" in names



def test_intent_change_updates_the_spec_instead_of_rewriting(engine, llm):
    engine.import_intent("A two-stage FIFO")
    run(engine, yes=True, stop="spec")
    engine.import_intent("A two-stage FIFO with a flush input")
    n = len(llm.calls)
    run(engine, yes=True, stop="spec")
    assert llm.names()[n:] == ["spec_update"]                         # not spec_write + spec_review
    assert "<new_intent>" in llm.calls[n].prompt and "flush input" in llm.calls[n].prompt


def test_a_spec_needs_no_intent_once_it_exists(engine, llm):
    """The intent only seeds the first draft: a copied project without spec/intent.md keeps its spec (no LLM, no 'missing input')."""
    engine.import_intent("A two-stage FIFO")
    run(engine, yes=True, stop="spec")
    (engine.project.spec_dir / "intent.md").unlink()
    assert {v.name: v.status for v in engine.status()}["spec"] in ("stale", "done")  # not missing_input
    n = len(llm.calls)
    run(engine, yes=True, stop="spec")
    assert len(llm.calls) == n and {v.name: v.status for v in engine.status()}["spec"] == "done"  # nothing to update: no LLM call
    assert (engine.project.spec_dir / "spec.md").is_file()


def test_reworded_repeats_of_settled_questions_are_dropped():
    from q3tui.steps.common import Question, drop_settled
    from tests.apb_questions import ARCH_Q, REQ_Q

    settled = [dict(q, answer=q["default_assumption"]) for q in REQ_Q]
    asked = [Question(id="", blocking=False, **q) for q in ARCH_Q] + [
        Question(id="", question="Should the DATA register use flops or a latch?", blocking=False, default_assumption="flops")]
    kept, dropped = drop_settled(asked, settled)
    assert [sid for _, sid in dropped] == ["Q-R1", "Q-R2", "Q-R3", "Q-R4"]
    assert [q.question for q in kept] == ["Should the DATA register use flops or a latch?"]


def test_errors_inside_task_groups_are_named():
    from q3tui.pipeline.base import StepFailed
    from q3tui.pipeline.engine import describe_error

    grp = BaseExceptionGroup("unhandled errors in a TaskGroup", [RuntimeError("model_x: schema"), StepFailed("rtl y failed")])
    assert describe_error(grp) == "RuntimeError: model_x: schema; rtl y failed"
    assert describe_error(ValueError("v")) == "ValueError: v"


def test_answer_all_takes_every_open_default(engine, llm):
    assert engine.answer_all() == "no open questions"
    engine.import_intent("x")
    run(engine)  # spec gate with Q-S1 open
    msg = engine.answer_all()
    assert "Q-S1" in msg and engine.open_question_count("spec") == 0
    q = next(q for q in engine.questions() if q["id"] == "Q-S1")
    assert q["status"] == "answered" and q["answer"] == "saturate"  # a real answer: the spec updates with it
    assert status(engine)["spec"][0] == "stale"


def test_every_question_comes_with_options(engine, llm):
    from q3tui.steps.common import derive_options

    assert [o["label"] for o in derive_options("Does it hold for the configuration?")] == ["yes", "no"]
    assert [o["label"] for o in derive_options("What is the depth?", "8")][:2] == ["8", "Decide for me"]
    engine.import_intent("x")
    run(engine)
    qs = engine.questions()
    assert qs and all(q["options"] for q in qs)


# -- the running set ------------------------------------------------------------------------------------------


def test_running_set_tracks_more_than_one_step(engine):
    assert engine.running is None and engine.running_steps == [] and not engine.is_running("rtl")
    engine._running = {"rtl", "tb"}
    assert engine.is_running("rtl") and engine.is_running("tb") and not engine.is_running("vplan")
    assert engine.running_steps == ["rtl", "tb"]
    assert engine.running in ("rtl", "tb")  # one representative name, for single-name messages
    engine.running = "req"  # the old str | None contract (TUI's quit-confirm, tests) still works
    assert engine.running_steps == ["req"]
    engine.running = None
    assert engine.running is None and engine.running_steps == []


def test_flow_session_reviewers_run_fresh(engine, llm, monkeypatch):
    """session: flow — every task goes to the flow's one session, except reviewers (roles.FLOW_FRESH: spec_review
    re-reads the whole draft and compacted the flow session at its first step) and the flow file's fresh_steps."""
    from q3tui.llm import roles
    from q3tui.llm.runtime import Stage
    from q3tui.pipeline.base import StepContext

    sent = []

    class Team:
        async def run(self, stage, role, cfg, emit):
            sent.append((stage.name, role))
            return await runtime.run_stage(stage, cfg, emit)

    monkeypatch.setattr(roles, "team", lambda project: Team())
    engine.project.cfg.pipeline.session = "flow"
    ctx = StepContext(engine, engine.bus.scoped("spec"))
    for name in ("spec_write", "spec_review"):
        anyio.run(ctx.llm, Stage(name=name, system_prompt="", prompt="", cwd=engine.project.root))
    assert sent == [("spec_write", "flow")]
    assert [c.name for c in llm.calls] == ["spec_write", "spec_review"]

    engine.flow.fresh_steps = ["spec"]  # the flow file's fresh_steps: every stage of the step runs fresh
    anyio.run(ctx.llm, Stage(name="spec_update", system_prompt="", prompt="", cwd=engine.project.root))
    assert sent == [("spec_write", "flow")] and llm.calls[-1].name == "spec_update"


def test_yes_approves_and_staleness(engine, llm):
    engine.import_intent("fifo")
    assert run(engine, yes=True) == "complete"
    assert status(engine)["spec"] == ("done", "approved") and status(engine)["rtl"][0] == "done"

    # a hand edit of the spec makes the steps reading it stale; the spec itself stays done (edited)
    spec = engine.project.spec_dir / "spec.md"
    spec.write_text(spec.read_text() + "\nMore detail.\n")
    st = status(engine)
    assert st["spec"][0] == "done" and st["rtl"][0] == "stale" and st["tb"][0] == "stale"
    assert [v.edited for v in engine.status() if v.name == "spec"][0] == ["spec/spec.md"]
    n = len(llm.calls)
    assert run(engine, yes=True) == "complete" and llm.names()[n:] == []   # only the code steps re-run
    assert status(engine)["rtl"][0] == "done"


def test_change_requests_rerun_the_step(engine, llm):
    engine.import_intent("fifo")
    run(engine, yes=True)
    engine.request_change("spec", "add a flush input")
    assert status(engine)["spec"] == ("stale", "open")
    n = len(llm.calls)
    run(engine, yes=True)
    assert llm.names()[n:] == ["spec_update"] and "add a flush input" in llm.calls[n].prompt
    assert engine.state.feedback["spec"] == [] and engine.state.feedback_history["spec"] == ["add a flush input"]
    assert status(engine)["spec"][0] == "done"


def test_user_spec_skips_spec_step(engine, llm, tmp_path):
    user_spec = tmp_path / "my_spec.md"
    user_spec.write_text("# my block\n")
    engine.import_spec([user_spec])
    assert status(engine)["spec"] == ("user", "approved")
    assert run(engine) == "complete"
    assert llm.names() == [] and status(engine)["rtl"][0] == "done"


def test_from_requires_upstream(engine, llm):
    engine.import_intent("x")
    assert run(engine, start="rtl") == "blocked"


def test_cancel_restores_previous_state(engine, llm, monkeypatch):
    engine.import_intent("fifo")
    run(engine, yes=True)
    engine.request_change("spec", "add a flush input")
    assert status(engine)["spec"][0] == "stale"

    async def cancelled(stage, cfg, emit):
        raise anyio.get_cancelled_exc_class()()

    monkeypatch.setattr("q3tui.llm.runtime.run_stage", cancelled)

    async def go():
        with anyio.CancelScope():
            try:
                await engine.run(yes=True)
            except anyio.get_cancelled_exc_class():
                pass

    anyio.run(go)
    assert status(engine)["spec"][0] == "stale"  # not "failed"
    assert engine.running is None


def test_reset_only_and_user_files_kept(engine, llm, tmp_path):
    user_spec = tmp_path / "mine.md"
    user_spec.write_text("# mine\n")
    engine.import_spec([user_spec])
    run(engine, yes=True)
    plan = engine.reset_plan("spec")
    assert all(f.name != "mine.md" for fs in plan.values() for f in fs)
    assert set(plan) == {"spec", "rtl", "tb"}

    engine.reset("rtl", only=True)
    assert (engine.project.spec_dir / "mine.md").exists()
    assert status(engine)["rtl"][0] == "pending" and status(engine)["tb"][0] == "done"


def test_approve_accepts_defaults_and_blocks_on_blocking(engine, llm):
    engine.import_intent("x")
    run(engine)  # spec gate; Q-S1 non-blocking with default "saturate"
    msg = engine.approve("spec")
    assert "accepted the default for Q-S1" in msg
    q = next(q for q in engine.questions() if q["id"] == "Q-S1")
    assert q["status"] == "default" and q["answer"] == "saturate"
    assert status(engine)["spec"] == ("done", "approved")  # not stale: nothing to regenerate
    assert engine.open_question_count("spec") == 0

    # changing an accepted default later behaves like a normal answer
    engine.answer("Q-S1", "wrap")
    assert next(q for q in engine.questions() if q["id"] == "Q-S1")["status"] == "answered"
    assert status(engine)["spec"][0] == "stale"

    engine.reset("spec")
    blocking_spec_question(engine, llm)
    run(engine)
    with pytest.raises(EngineError, match="Q-S1"):
        engine.approve("spec")
    assert "approved spec" in engine.approve("spec", force=True)


def test_auto_answer_runs_without_stopping(engine, llm):
    engine.project.cfg.pipeline.auto_answer = True
    blocking_spec_question(engine, llm)
    engine.import_intent("fifo")
    assert run(engine) == "complete"               # through the blocking Q-S1 (its default)
    q = next(q for q in engine.questions() if q["id"] == "Q-S1")
    assert q["status"] != "open" and status(engine)["spec"] == ("done", "approved")


def test_approve_all_skips_blocking(engine, llm):
    blocking_spec_question(engine, llm)
    engine.import_intent("x")
    run(engine)  # spec gate open, Q-S1 blocking
    assert engine.pending_approvals() == ["spec"]
    lines = engine.approve_all()
    assert lines[0].startswith("skipped spec") and "Q-S1" in lines[0]
    assert status(engine)["spec"][1] == "open"
    assert engine.approve_all(force=True)[0].startswith("approved spec")
    assert engine.approve_all() == ["nothing waiting for review"]


def test_a_step_left_running_by_a_killed_process_is_run_again_not_yours(engine, llm):
    engine.import_intent("fifo")
    run(engine, yes=True, stop="rtl")
    engine.state.steps["rtl"].status = "running"          # the process died during rtl
    engine.save()
    fresh = Engine(Project.open(engine.project.root))
    v = fresh.evaluate(fresh.by_name["rtl"])
    assert v.status == "stale" and "interrupted" in v.detail
    run(fresh, yes=True, stop="rtl")
    assert fresh.state.steps["rtl"].origin == "generated" and fresh.state.steps["rtl"].status == "done"


def test_a_steps_cost_adds_up_over_its_runs(engine, llm):
    """`q3tui cost` showed only the last run of a step (the first run's cost vanished from the per-step list)."""
    engine.import_intent("fifo")
    run(engine, yes=True, stop="spec")
    first = engine.state.steps["spec"].cost_usd
    assert first > 0
    engine.request_change("spec", "tighten the overview")
    run(engine, yes=True, stop="spec")
    rec = engine.state.steps["spec"]
    assert rec.cost_usd > first and rec.cost_usd == pytest.approx(engine.state.total_cost_usd)
    assert engine.cost_summary()["steps"]["spec"] == pytest.approx(rec.cost_usd)
