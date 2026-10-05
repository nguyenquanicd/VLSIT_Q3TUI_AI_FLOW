"""M10 role sessions (docs/spec/team.md): every role task goes to its role's one persistent session — init once,
a stable charter, instructions and large blocks sent once, parallel tasks fork, a compaction brings the brief back."""

import anyio
import pytest

from q3tui.llm import roles, runtime
from q3tui.llm.runtime import Stage, StageResult
from q3tui.pipeline.engine import Engine
from q3tui.core.project import Project
from tests.fakes import FakeLLM, use_test_flow


@pytest.fixture
def llm(monkeypatch):
    fake = FakeLLM()
    monkeypatch.setattr(runtime, "run_stage", fake)
    return fake


@pytest.fixture
def engine(tmp_path):
    (tmp_path / "q3tui.yaml").write_text("req: {self_review: false}\ntriage: {investigate: false}\n"
                                             "pipeline: {auto_approve: true}\n"
                                             "team: {enabled: true, unit_sessions: role}\n")  # (M10: every task in its role)
    yield Engine(Project.open(tmp_path))
    roles.forget(Project.open(tmp_path))


def test_tasks_belong_to_roles_and_reviewers_stay_independent():
    assert roles.role_of("req_build") == roles.role_of("arch_agent") == roles.role_of("spec_write") == "architect"
    assert roles.role_of("model_sync_fifo") == "modeler" and roles.role_of("rtl_sync_fifo") == "designer"
    assert roles.role_of("tb_checker") == roles.role_of("unit_tb_stimulus") == roles.role_of("triage") == "verifier"
    assert roles.role_of("vplan_write") == roles.role_of("unit_vplan_write") == "verifier"
    assert roles.role_of("rtl_review_sync_fifo") == "reviewer"                     # its own session, not the designer's
    for fresh in ("req_review", "spec_review", "arch_block_fifo", "req_section_overview",
                  "vplan_group_REQ-001", "triage_rtl", "team_designer_init"):
        assert roles.role_of(fresh) is None, fresh


def test_the_pipeline_runs_in_persistent_role_sessions(tmp_path, llm):
    use_test_flow(tmp_path, "team: {enabled: true, unit_sessions: role}\n")  # (M10: every task in its role)
    engine = Engine(Project.open(tmp_path))
    engine.import_intent("fifo")
    anyio.run(lambda: engine.run(yes=True))
    engine.request_change("spec", "add a flush input")
    anyio.run(lambda: engine.run(yes=True))
    tasks = [c for c in llm.calls if c.role]
    assert [c.name for c in tasks] == ["spec_write", "spec_update"] and {c.role for c in tasks} == {"architect"}
    assert all(c.system_prompt == roles.CHARTERS[c.role] for c in tasks)              # a stable charter, per role
    assert tasks[0].resume is None and "# Project brief" in tasks[0].prompt             # init once: the first task has the brief
    assert tasks[1].resume == "sess-1" and "# Project brief" not in tasks[1].prompt
    assert all(c.builtin_tools == roles.ROLE_BUILTINS[c.role] for c in tasks)     # a stable tool list
    assert all(c.role is None for c in llm.calls if c.name == "spec_review")          # reviewers stay fresh
    t = roles.team(engine.project)
    assert t.roles["architect"].tasks == 2 and (engine.project.state_dir / "team" / "roles.json").is_file()
    roles.forget(engine.project)


class Recorder:
    """A run_stage that records what each task was sent (and can pretend the session was compacted)."""

    def __init__(self):
        self.stages: list[Stage] = []
        self.compact_next = False

    async def __call__(self, stage, cfg, emit):
        self.stages.append(stage)
        if self.compact_next and stage.on_compact:
            self.compact_next = False
            stage.on_compact()
        sid = f"fork-{len(self.stages)}" if stage.fork else "main"
        return StageResult(f"did {stage.name}", None, 0.01, 1, sid, last_uuid=f"end-of-{stage.name}")


class Emit:
    step = "rtl"

    def __call__(self, *a, **k):
        pass

    def add_cost(self, c):
        pass


def test_instructions_and_big_blocks_are_sent_once_and_a_compaction_brings_the_brief_back(tmp_path, monkeypatch):
    (tmp_path / "q3tui.yaml").write_text("team: {enabled: true, dedupe_chars: 50}\n")
    project = Project.open(tmp_path)
    rec = Recorder()
    monkeypatch.setattr(runtime, "run_stage", rec)
    t = roles.Team(project)
    big = "- REQ-001 the FIFO keeps order " * 5

    def task(module):
        return Stage(name=f"rtl_{module}", system_prompt="Write the module well. " * 5, prompt=f"Module {module}\n\n{big}",
                     cwd=tmp_path, builtin_tools=["Read", "Write"])

    anyio.run(lambda: t.run(task("a"), "designer", project.cfg, Emit()))
    anyio.run(lambda: t.run(task("b"), "designer", project.cfg, Emit()))
    first, second = rec.stages
    assert first.resume is None and "Project brief" in first.prompt and "Project brief" not in second.prompt
    assert "Write the module well." in first.prompt and big in first.prompt
    assert "the same as for your earlier `rtl` tasks" in second.prompt and big not in second.prompt
    assert "unchanged, as sent to you earlier" in second.prompt and "Module b" in second.prompt

    rec.compact_next = True                                                          # Claude compacts during a task
    anyio.run(lambda: t.run(task("c"), "designer", project.cfg, Emit()))
    anyio.run(lambda: t.run(task("d"), "designer", project.cfg, Emit()))
    after = rec.stages[-1]
    assert "Project brief" in after.prompt and big in after.prompt                  # the brief again, blocks sent again
    assert t.roles["designer"].compactions == 1

    # a restarted process resumes the same session (roles.json), without a new init
    t2 = roles.Team(project)
    anyio.run(lambda: t2.run(task("e"), "designer", project.cfg, Emit()))
    assert rec.stages[-1].resume == "main" and not rec.stages[-1].name.endswith("_init")


@pytest.mark.parametrize("fork", [False, True])
def test_parallel_tasks_queue_for_the_session_or_fork_it(tmp_path, monkeypatch, fork):
    (tmp_path / "q3tui.yaml").write_text(f"team: {{enabled: true, fork: {str(fork).lower()}}}\n")
    project = Project.open(tmp_path)
    rec = Recorder()
    order = []

    async def run_stage(stage, cfg, emit):
        order.append(f"start {stage.name}")
        if stage.name == "rtl_a":
            await anyio.sleep(0.3)
        order.append(f"end {stage.name}")
        return await rec(stage, cfg, emit)

    monkeypatch.setattr(runtime, "run_stage", run_stage)
    t = roles.Team(project)

    def task(m):
        return Stage(name=f"rtl_{m}", system_prompt="x", prompt=m, cwd=tmp_path, builtin_tools=["Read"])

    async def both():
        await t.run(task("w"), "designer", project.cfg, Emit())                    # (the session exists)
        async with anyio.create_task_group() as tg:
            tg.start_soon(t.run, task("a"), "designer", project.cfg, Emit())
            await anyio.sleep(0.05)
            tg.start_soon(t.run, task("b"), "designer", project.cfg, Emit())
        await t.run(task("c"), "designer", project.cfg, Emit())

    anyio.run(both)
    by = {s.name: s for s in rec.stages}
    assert by["rtl_a"].resume == "main" and not by["rtl_a"].fork
    if not fork:  # b waited for a, then ran on the main session
        assert order.index("start rtl_b") > order.index("end rtl_a") and not by["rtl_b"].fork
        assert t.roles["designer"].forks == 0
    else:         # b forked the busy session; its summary reaches the main session with the next task
        assert order.index("start rtl_b") < order.index("end rtl_a") and by["rtl_b"].fork
        assert by["rtl_b"].fork_at == "end-of-rtl_w"     # branched at the last idle point, never inside rtl_a
        assert "rtl_b: did rtl_b" in by["rtl_c"].prompt and t.roles["designer"].forks == 1
    assert t.roles["designer"].session == "main"


def test_a_task_submits_its_result_through_the_stable_submit_tool(tmp_path, monkeypatch):
    from pydantic import BaseModel

    class Out(BaseModel):
        summary: str
        n: int

    (tmp_path / "q3tui.yaml").write_text("team: {enabled: true}\n")
    project = Project.open(tmp_path)
    seen = []

    async def run_stage(stage, cfg, emit):
        seen.append(stage)
        if stage.role and not stage.name.endswith("_init"):
            submit = next(x for x in stage.sdk_tools if x.name == "submit")
            bad = await submit.handler({"result": {"summary": "x"}})
            assert "not accepted" in bad["content"][0]["text"] and "n: Field required" in bad["content"][0]["text"]
            await submit.handler({"result": {"summary": "x", "n": 3}})
        return StageResult("done", None, 0.01, 1, "main")

    monkeypatch.setattr(runtime, "run_stage", run_stage)
    t = roles.Team(project)
    st = Stage(name="rtl_a", system_prompt="x", prompt="go", cwd=tmp_path, output_model=Out)
    res = anyio.run(lambda: t.run(st, "designer", project.cfg, Emit()))
    assert res.output == Out(summary="x", n=3)
    task = seen[-1]
    assert task.submit_tool and '"n"' in task.prompt and "call `submit`" in task.prompt
    names = [x.name for x in task.sdk_tools]
    assert names[:2] == ["notes", "submit"] and [x.name for x in seen[0].sdk_tools][:2] == ["notes", "submit"]


def test_a_tool_of_another_task_answers_that_it_is_not_available(tmp_path, monkeypatch):
    from claude_agent_sdk import tool

    (tmp_path / "q3tui.yaml").write_text("team: {enabled: true}\n")
    project = Project.open(tmp_path)
    rec = Recorder()
    monkeypatch.setattr(runtime, "run_stage", rec)
    t = roles.Team(project)

    @tool("compile", "Compile.", {})
    async def compile_tool(args):
        return {"content": [{"type": "text", "text": "clean"}]}

    @tool("probe", "Probe.", {})
    async def probe_tool(args):
        return {"content": [{"type": "text", "text": "probed"}]}

    s1 = Stage(name="rtl_a", system_prompt="x", prompt="a", cwd=tmp_path, sdk_tools=[compile_tool, probe_tool])
    s2 = Stage(name="rtl_b", system_prompt="x", prompt="b", cwd=tmp_path, sdk_tools=[compile_tool])
    anyio.run(lambda: t.run(s1, "designer", project.cfg, Emit()))
    anyio.run(lambda: t.run(s2, "designer", project.cfg, Emit()))
    offered = {x.name: x for x in rec.stages[-1].sdk_tools}
    assert list(offered) == ["notes", "submit", "compile", "probe"]                  # the same list as before
    out = anyio.run(lambda: offered["probe"].handler({}))
    assert "not available in this task" in out["content"][0]["text"]


def test_the_hook_denies_built_in_tools_the_task_did_not_ask_for_but_never_the_notes(tmp_path):
    notes = tmp_path / "rtl" / "NOTES.md"
    st = Stage(name="triage", system_prompt="", prompt="", cwd=tmp_path, builtin_tools=roles.ROLE_BUILTINS["designer"],
               task_tools=[], role="verifier", notes_file=notes)
    hook = runtime._path_guard(st, None)
    denied = anyio.run(lambda: hook({"tool_name": "Read", "tool_input": {"file_path": str(tmp_path / "x.sv")}}, None, None))
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    ok = anyio.run(lambda: hook({"tool_name": "Write", "tool_input": {"file_path": str(notes)}}, None, None))
    assert ok == {}


def test_a_task_without_file_tools_may_read_its_roles_area_but_never_the_other_side(tmp_path, monkeypatch):
    (tmp_path / "q3tui.yaml").write_text("team: {enabled: true}\n")
    project = Project.open(tmp_path)
    rec = Recorder()
    monkeypatch.setattr(runtime, "run_stage", rec)
    t = roles.Team(project)
    triage = Stage(name="triage", system_prompt="judge", prompt="evidence", cwd=tmp_path, builtin_tools=[])
    anyio.run(lambda: t.run(triage, "verifier", project.cfg, Emit()))
    task = rec.stages[-1]
    assert {"Read", "Grep", "Glob"} <= set(task.task_tools) and not {"Write", "Edit"} & set(task.task_tools)
    assert project.rtl_dir in task.deny_dirs and project.model_dir in task.deny_dirs
    assert "Tools for this task: Read, Grep, Glob (read only" in task.prompt


def test_a_task_that_fails_in_its_role_session_is_done_once_in_a_fresh_session(tmp_path, monkeypatch):
    (tmp_path / "q3tui.yaml").write_text("team: {enabled: true}\n")
    project = Project.open(tmp_path)
    rec = Recorder()

    async def run_stage(stage, cfg, emit):
        if stage.role and stage.name == "spec_update":
            raise runtime.StageError("spec_update: invalid structured output", 0.05)
        return await rec(stage, cfg, emit)

    monkeypatch.setattr(runtime, "run_stage", run_stage)
    t = roles.Team(project)
    up = Stage(name="spec_update", system_prompt="update", prompt="the sections", cwd=tmp_path, builtin_tools=[])
    res = anyio.run(lambda: t.run(up, "architect", project.cfg, Emit()))
    fresh = rec.stages[-1]
    assert fresh.role is None and fresh.prompt == "the sections" and res.cost_usd == pytest.approx(0.06)
    nxt = Stage(name="req_update", system_prompt="req", prompt="go", cwd=tmp_path, builtin_tools=[])
    anyio.run(lambda: t.run(nxt, "architect", project.cfg, Emit()))
    assert "spec_update: failed in your session and was done by a fresh session" in rec.stages[-1].prompt


def test_every_task_can_keep_notes_and_only_writing_roles_get_write_tools(tmp_path, monkeypatch):
    (tmp_path / "q3tui.yaml").write_text("team: {enabled: true}\n")
    project = Project.open(tmp_path)
    rec = Recorder()
    monkeypatch.setattr(runtime, "run_stage", rec)
    t = roles.Team(project)
    up = Stage(name="spec_update", system_prompt="update", prompt="sections", cwd=tmp_path, builtin_tools=[])
    anyio.run(lambda: t.run(up, "architect", project.cfg, Emit()))
    task = rec.stages[-1]
    assert "Edit" not in task.builtin_tools and "Write" not in task.builtin_tools
    notes = next(x for x in task.sdk_tools if x.name == "notes")
    anyio.run(lambda: notes.handler({"text": "latency is 2 cycles"}))
    assert roles.notes_path(project, "architect").read_text() == "latency is 2 cycles\n"


def test_a_judge_that_may_now_read_gets_the_turns_for_it(tmp_path, monkeypatch):
    (tmp_path / "q3tui.yaml").write_text("team: {enabled: true}\n")
    project = Project.open(tmp_path)
    rec = Recorder()
    monkeypatch.setattr(runtime, "run_stage", rec)
    t = roles.Team(project)
    anyio.run(lambda: t.run(Stage(name="triage", system_prompt="judge", prompt="e", cwd=tmp_path, builtin_tools=[], max_turns=4),
                            "verifier", project.cfg, Emit()))
    assert rec.stages[-1].max_turns == 10


def test_a_session_grown_too_big_is_compacted_not_replaced(tmp_path, monkeypatch):
    """rv32im: a verifier task grew its session to 270k tokens. Past team.max_context the SAME session is compacted
    (one session per role, its memory kept as a summary); the next task gets the brief and the notes again."""
    (tmp_path / "q3tui.yaml").write_text("team: {enabled: true, max_context: 10000}\n")
    project = Project.open(tmp_path)
    sizes = iter([5_000, 50_000, 0, 5_000])
    seen = []

    async def run_stage(stage, cfg, emit):
        seen.append(stage)
        if stage.prompt.startswith("/compact") and stage.on_compact:
            stage.on_compact()
        return StageResult("ok", None, 0.01, 1, "s1", None, next(sizes))

    monkeypatch.setattr(runtime, "run_stage", run_stage)
    t = roles.Team(project)
    for m in ("a", "b", "c"):
        anyio.run(lambda: t.run(Stage(name=f"rtl_{m}", system_prompt="x", prompt=m, cwd=tmp_path), "designer",
                                project.cfg, Emit()))
    names = [s.name for s in seen]
    assert names == ["rtl_a", "rtl_b", "team_designer_compact", "rtl_c"]
    assert seen[2].resume == "s1" and seen[2].prompt.startswith("/compact ")
    assert seen[3].resume == "s1" and "# Project brief" in seen[3].prompt           # the same session, re-briefed
    assert t.roles["designer"].compactions == 1 and t.roles["designer"].rotations == 0


def test_a_document_the_role_wrote_is_referenced_not_re_sent(tmp_path, monkeypatch):
    """rv32im: the architect's session wrote the spec; the next task (requirements) re-sent all 35k characters of it
    (spec paragraphs are shorter than any threshold, and what a session writes itself was never counted as sent).
    Now: markdown sections are the unit, and the role's own products count as in its session."""
    (tmp_path / "q3tui.yaml").write_text("team: {enabled: true, dedupe_chars: 40}\n")
    project = Project.open(tmp_path)
    rec = Recorder()
    monkeypatch.setattr(runtime, "run_stage", rec)
    t = roles.Team(project)
    sec = lambda n, body: f"## {n}. Section {n}\n\n{body}"                                        # noqa: E731
    spec = "\n".join(sec(i, f"The core does thing number {i} in a precise, testable way.") for i in range(1, 4))
    (tmp_path / "spec").mkdir(exist_ok=True)

    def task(name, prompt):
        return Stage(name=name, system_prompt="x", prompt=prompt, cwd=tmp_path, builtin_tools=["Read"])

    (tmp_path / "spec" / "spec.md").write_text(spec)                      # what spec_write produced (through its tools)
    anyio.run(lambda: t.run(task("spec_write", "Write the spec."), "architect", project.cfg, Emit()))
    changed = spec.replace("thing number 2", "thing number 22")
    anyio.run(lambda: t.run(task("req_build", f"# Requirements task\n{changed}"), "architect", project.cfg, Emit()))
    p = rec.stages[-1].prompt
    assert "thing number 22" in p                                          # the changed section: in full
    assert "thing number 1 " not in p and "thing number 3 " not in p       # the unchanged ones: references
    assert p.count("[unchanged, as sent to you earlier in this session") == 2


def test_notes_are_updated_by_section_not_rewritten_whole():
    """rv32im modeler: the notes tool only rewrote the whole file — appending one block's section wiped the others
    ("I made the same mistake again — the notes tool overwrites the whole file") and every block re-sent them all."""
    from q3tui.llm.roles import set_section

    doc = set_section("", "alu", "adds and subtracts")
    doc = set_section(doc, "regfile", "x0 reads 0")
    doc = set_section(doc, "ALU", "## alu\nnow also shifts")                 # replaced in place (its own heading dropped)
    assert doc == "## ALU\n\nnow also shifts\n\n## regfile\n\nx0 reads 0\n"
    assert set_section("# Notes\nintro\n", "div", "restores").startswith("# Notes\nintro\n\n## div")
