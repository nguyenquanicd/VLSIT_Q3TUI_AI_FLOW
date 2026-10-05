"""run_stage against a fake SDK `query`: structured-output retry in the same session."""

import anyio
import pytest
from claude_agent_sdk import ResultMessage
from pydantic import BaseModel

from q3tui.core.config import Config
from q3tui.core.events import EventBus
from q3tui.llm import runtime
from q3tui.llm.runtime import Stage, StageError


class Out(BaseModel):
    title: str
    n: int


def result(data, session="s1", cost=0.1):
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                         session_id=session, total_cost_usd=cost, structured_output=data, result="")


def fake_query(outputs, seen):
    spent = {}

    async def query(prompt, options):  # like the SDK: a resumed session reports its running total
        seen.append((prompt, options.resume))
        spent["s1"] = spent.get("s1", 0.0) + 0.1
        yield result(outputs.pop(0), cost=spent["s1"])

    return query


def run(stage):
    bus = EventBus()
    warnings = []
    bus.subscribe(lambda e: warnings.append(e) if e.kind == "warning" else None)
    return anyio.run(lambda: runtime.run_stage(stage, Config(), bus.scoped("t"))), warnings


def stage(tmp_path):
    return Stage(name="t", system_prompt="s", prompt="go", cwd=tmp_path, output_model=Out)


def test_retry_fixes_output(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(runtime, "query", fake_query([{"n": 1}, {"title": "x", "n": 1}], seen))
    res, warnings = run(stage(tmp_path))
    assert res.output == Out(title="x", n=1)
    assert res.cost_usd == pytest.approx(0.2)
    assert seen[0] == ("go", None)
    assert seen[1][1] == "s1" and "title: Field required" in seen[1][0]
    assert len(warnings) == 1


def test_gives_up_after_retries(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(runtime, "query", fake_query([{"n": 1}] * 3, seen))
    with pytest.raises(StageError) as exc:
        run(stage(tmp_path))
    assert exc.value.cost_usd == pytest.approx(0.3) and len(seen) == 3


def test_path_guard(tmp_path):
    from q3tui.llm.runtime import check_tool_paths

    proj = tmp_path / "proj"
    (proj / "tb").mkdir(parents=True)
    extra = tmp_path / "ip"
    st = Stage(name="t", system_prompt="", prompt="", cwd=proj, add_dirs=[extra], deny_dirs=[proj / "tb"])
    assert check_tool_paths(st, {"file_path": "spec/spec.md"}) is None
    assert check_tool_paths(st, {"file_path": str(extra / "x.sv")}) is None
    assert check_tool_paths(st, {"pattern": "**/*.sv"}) is None
    assert "outside the project" in check_tool_paths(st, {"file_path": "/etc/passwd"})
    assert "outside the project" in check_tool_paths(st, {"file_path": "../other/x"})
    assert "outside the project" in check_tool_paths(st, {"path": str(tmp_path)})
    assert "outside the project" in check_tool_paths(st, {"pattern": "/home/**/*.md"})
    assert "not accessible" in check_tool_paths(st, {"file_path": "tb/tb.sv"})


def test_progress_not_logged(tmp_path):
    bus = EventBus()
    bus.log_to(tmp_path / "e.jsonl")
    got = []
    bus.subscribe(got.append)
    bus.emit("progress", "spec", thinking_tokens=100)
    bus.emit("log", "spec", message="x")
    assert [e.kind for e in got] == ["progress", "log"]
    assert "progress" not in (tmp_path / "e.jsonl").read_text()


def test_state_dir_hidden_from_llm(tmp_path, monkeypatch):
    """Backups/logs in .q3tui/ must not feed a stage (reset → rerun would reuse them)."""
    from q3tui.llm.runtime import build_options, check_tool_paths
    from q3tui.pipeline.engine import Engine
    from q3tui.core.project import Project

    (tmp_path / "q3tui.yaml").write_text("pipeline: {session: fresh}\n")  # (a stage of its own, not the flow's session)
    engine = Engine(Project.open(tmp_path))
    engine.save()
    assert (tmp_path / ".q3tui" / ".ignore").read_text().strip().endswith("*")

    seen = []

    async def fake(stage, cfg, emit):
        seen.append(stage)
        from q3tui.llm.runtime import StageResult
        return StageResult("", None, 0.0, 1, None)

    monkeypatch.setattr(runtime, "run_stage", fake)
    from q3tui.pipeline.base import StepContext

    ctx = StepContext(engine, engine.bus.scoped("spec"))
    st = Stage(name="x", system_prompt="s", prompt="p", cwd=tmp_path)
    anyio.run(lambda: ctx.llm(st))
    assert seen[0].deny_dirs == [tmp_path / ".q3tui"]
    bkp = tmp_path / ".q3tui" / "bkp" / "1" / "spec" / "spec.md"
    assert "not accessible" in check_tool_paths(seen[0], {"file_path": str(bkp)})
    assert check_tool_paths(seen[0], {"file_path": "spec/spec.md"}) is None
    assert ".q3tui" in build_options(seen[0], Config()).system_prompt


def test_thinking_is_capped_only_without_effort(tmp_path):
    from q3tui.llm.runtime import build_options

    st = Stage(name="s", system_prompt="", prompt="", cwd=tmp_path)
    cfg = Config()
    assert build_options(st, cfg).thinking is None                       # effort (Opus/Sonnet) steers thinking
    cfg.llm.effort = None                                                # Haiku 4.5: no effort setting
    assert build_options(st, cfg).thinking == {"type": "enabled", "budget_tokens": 6000}
    cfg.llm.thinking_budget = 0
    assert build_options(st, cfg).thinking == {"type": "disabled"}
    cfg.llm.thinking_budget = None
    assert build_options(st, cfg).thinking is None


def test_glob_never_lists_backups_or_denied_dirs(tmp_path):
    from q3tui.llm.runtime import build_options, safe_glob

    for f in ("spec/spec.md", "model/tests/test_a.py", ".q3tui/bkp/1/model/tests/test_a.py", "rtl/top.sv",
              ".git/config", "model/tests/__pycache__/x.pyc"):
        (tmp_path / f).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / f).write_text("x")
    st = Stage(name="s", system_prompt="", prompt="", cwd=tmp_path, builtin_tools=["Read", "Glob"],
               deny_dirs=[tmp_path / ".q3tui", tmp_path / "rtl"])
    assert safe_glob(st, "**/test_*.py") == "model/tests/test_a.py"          # the backup copy is not listed
    assert safe_glob(st, "**/*").splitlines() == ["model/tests/test_a.py", "spec/spec.md"]
    assert "not accessible" in safe_glob(st, "*", ".q3tui/bkp")
    assert "not accessible" in safe_glob(st, "*", "/etc")
    opts = build_options(st, Config())
    assert "Glob" not in opts.tools and "mcp__q3tui__glob" in opts.allowed_tools


def test_write_files_limits_writes_to_exact_files(tmp_path):
    from q3tui.llm.runtime import check_tool_paths

    st = Stage(name="m", system_prompt="", prompt="", cwd=tmp_path, write_dirs=[tmp_path / "model"],
               write_files=[tmp_path / "model/blocks/a.py", tmp_path / "model/tests/test_a.py"])
    assert check_tool_paths(st, {"file_path": "model/blocks/a.py"}, "Edit") is None
    assert check_tool_paths(st, {"file_path": "model/tests/test_a.py"}, "Write") is None
    assert "may only write" in check_tool_paths(st, {"file_path": "model/tests/setup.cfg"}, "Write")
    assert check_tool_paths(st, {"file_path": "model/tests/setup.cfg"}, "Read") is None          # reading is fine


def test_tool_output_is_capped():
    import anyio
    from claude_agent_sdk import tool

    from q3tui.llm.runtime import capped

    @tool("big", "returns a lot", {})
    async def big(args):
        return {"content": [{"type": "text", "text": "A" * 5000 + "MIDDLE" + "Z" * 5000}]}

    out = anyio.run(lambda: capped(big, 3000).handler({}))
    text = out["content"][0]["text"]
    assert len(text) < 3200 and text.startswith("AAA") and text.endswith("ZZZ") and "characters cut by Q3TUI" in text
    small = anyio.run(lambda: capped(big, 20000).handler({}))
    assert small["content"][0]["text"].count("MIDDLE") == 1


def test_each_step_can_use_its_own_model(tmp_path):
    from q3tui.core.config import Config
    from q3tui.core.events import EventBus
    from q3tui.llm.runtime import Stage, build_options

    cfg = Config.model_validate({"llm": {"model": "claude-sonnet-5-5", "effort": "high", "steps": {
        "doc": {"model": "claude-haiku-4-5"}, "tb": {"effort": "medium"}, "verify": {"model": "claude-opus-5-5"}}}})
    llm = cfg.llm
    assert (llm.for_stage("doc", "doc_prose").model, llm.for_stage("doc", "doc_prose").effort) == ("claude-haiku-4-5", None)
    assert (llm.for_stage("tb", "tb_plan").model, llm.for_stage("tb", "tb_plan").effort) == ("claude-sonnet-5-5", "medium")
    assert (llm.for_stage("verify", "verify_triage").model, llm.for_stage("verify", "verify_triage").effort) == ("claude-opus-5-5", "high")
    assert llm.for_stage("parse", "parse_spec") is llm                                   # nothing set: the global config
    opts = build_options(Stage(name="doc_prose", system_prompt="s", prompt="p", cwd=tmp_path), cfg, EventBus().scoped("doc"))
    assert opts.model == "claude-haiku-4-5" and opts.effort is None


def test_only_fields_without_defaults_are_required():
    from q3tui.llm.runtime import output_schema

    class Notes(BaseModel):
        untestable: list[str] = []

    class Bug(BaseModel):
        verdict: str
        fix: str
        spec_expectation: str
        confidence: str = "high"

    class Verdicts(BaseModel):
        bugs: list[Bug]

    assert output_schema(Notes)["required"] == []                         # leaving out `untestable` must not fail
    bug = output_schema(Verdicts)["properties"]["bugs"]["items"]
    assert {"verdict", "fix", "spec_expectation"} <= set(bug["required"]) and bug["additionalProperties"] is False


def test_grep_cannot_reach_into_denied_folders(tmp_path):
    from q3tui.llm.runtime import Stage, check_tool_paths

    for d in ("rtl", "model", "tb", "spec"):
        (tmp_path / d).mkdir()
    stage = Stage(name="rtl_x", system_prompt="s", prompt="p", cwd=tmp_path, deny_dirs=[tmp_path / "model", tmp_path / "tb"])
    reason = check_tool_paths(stage, {"pattern": "REQ-001"}, "Grep")               # no path: the whole project
    assert reason and "model, tb" in reason and "rtl, spec" in reason
    assert check_tool_paths(stage, {"pattern": "REQ-001", "path": str(tmp_path / "rtl")}, "Grep") is None
    assert "not accessible" in check_tool_paths(stage, {"pattern": "x", "path": "model"}, "Grep")


def test_hitting_the_output_limit_just_continues_at_the_same_effort(monkeypatch, tmp_path):
    """Like Claude Code: the SDK continues after "Output token limit hit" — no restart, no lower effort."""
    from claude_agent_sdk import AssistantMessage, ThinkingBlock, UserMessage

    seen = []

    async def query(prompt, options):
        seen.append(options.effort)
        yield AssistantMessage(content=[ThinkingBlock(thinking="", signature="x")], model="m")
        yield UserMessage(content=[{"type": "text", "text": "Output token limit hit. Resume directly"}])
        yield result({"title": "x", "n": 1})

    monkeypatch.setattr(runtime, "query", query)
    cfg = Config.model_validate({"llm": {"effort": "high"}})
    res = anyio.run(lambda: runtime.run_stage(stage(tmp_path), cfg, EventBus().scoped("t")))
    assert res.output == Out(title="x", n=1) and seen == ["high"]


def test_a_text_answer_is_the_summary_when_nothing_else_is_required(monkeypatch, tmp_path):
    class ModuleResult(BaseModel):
        summary: str
        requirements: list[str] = []
        questions: list[str] = []

    seen = []

    async def query(prompt, options):
        seen.append(prompt)
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=3,
                            session_id="s1", total_cost_usd=0.1, structured_output=None, result="Implemented; compile is clean.")

    monkeypatch.setattr(runtime, "query", query)
    st = Stage(name="rtl_x", system_prompt="s", prompt="go", cwd=tmp_path, output_model=ModuleResult)
    res, warnings = run(st)
    assert res.output.summary == "Implemented; compile is clean." and len(seen) == 1 and not warnings   # no re-ask


def test_long_thinking_is_never_cut_short(monkeypatch, tmp_path):
    """Like Claude Code: the model decides how long it thinks. The old 16k-token guard restarted Sonnet's hard
    diagnoses at lower effort and remembered it — rv32im's designer, modeler and debugger ended at "low" for good."""
    from claude_agent_sdk import SystemMessage

    seen = []

    async def query(prompt, options):
        seen.append(options.effort)
        yield SystemMessage(subtype="thinking_tokens", data={"estimated_tokens": 60_000})
        yield result({"title": "x", "n": 1})

    monkeypatch.setattr(runtime, "query", query)
    cfg = Config.model_validate({"llm": {"effort": "high", "runaway_thinking_tokens": 32_000}})   # old configs load
    res = anyio.run(lambda: runtime.run_stage(stage(tmp_path), cfg, EventBus().scoped("t")))
    assert seen == ["high"] and res.output == Out(title="x", n=1)


def test_a_stage_that_reads_files_is_not_reused_after_they_change(tmp_path):
    import os

    from q3tui.llm.runtime import Stage
    from q3tui.pipeline.base import _readable_digest

    (tmp_path / "rtl").mkdir()
    f = tmp_path / "rtl" / "top.sv"
    f.write_text("module top; endmodule\n")
    (tmp_path / ".q3tui").mkdir()
    st = Stage(name="rtl_review_top", system_prompt="", prompt="review rtl/top.sv", cwd=tmp_path, builtin_tools=["Read"])
    before = _readable_digest(st)
    (tmp_path / ".q3tui" / "log").write_text("x")                        # hidden folders do not count
    assert _readable_digest(st) == before
    f.write_text("module top; logic a; endmodule\n")
    os.utime(f, ns=(1, 1))
    assert _readable_digest(st) != before


def test_a_resumed_session_is_charged_only_for_the_new_query(monkeypatch, tmp_path):
    """The SDK reports a resumed (or forked) session's running total: only the difference is this stage's cost."""
    totals = iter([0.30, 0.45])

    async def query(prompt, options):
        yield result({"title": "x", "n": 1}, session="main", cost=next(totals))

    monkeypatch.setattr(runtime, "query", query)
    first, _ = run(stage(tmp_path))
    st = stage(tmp_path)
    st.resume = "main"
    second, _ = run(st)
    assert first.cost_usd == pytest.approx(0.30) and second.cost_usd == pytest.approx(0.15)


def test_when_the_structured_output_gives_up_the_result_comes_as_a_json_block(monkeypatch, tmp_path):
    """Haiku breaks the SDK's structured-output JSON on long Markdown values: the stage is asked once more for a JSON
    block in its reply, parsed leniently (a raw newline inside a string is fine) and validated here."""
    seen = []

    async def query(prompt, options):
        seen.append((prompt, options.output_format))
        if len(seen) == 1:
            yield ResultMessage(subtype="error_during_execution", duration_ms=1, duration_api_ms=1, is_error=True, num_turns=5,
                                session_id="s1", total_cost_usd=0.05, result="Failed to provide valid structured output after 5 attempts")
        else:
            yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1, session_id="s2",
                                total_cost_usd=0.02, structured_output=None,
                                result='Here it is:\n```json\n{"title": "line one\nline two", "n": 2}\n```')

    monkeypatch.setattr(runtime, "query", query)
    res, warnings = run(stage(tmp_path))
    assert res.output == Out(title="line one\nline two", n=2) and res.cost_usd == pytest.approx(0.07)
    assert seen[0][1] is not None and seen[1][1] is None                       # no SDK structured output the second time
    assert "```json" in seen[1][0] and '"title"' in seen[1][0]                 # the schema is in the prompt
    assert any("asking for the result as a JSON block" in w.data["message"] for w in warnings)
