"""Flows as data: loader, validation, gate modes, custom flows through the engine (docs/spec/flows.md)."""

import anyio
import pytest
import yaml
from click.testing import CliRunner

from q3tui import flows
from q3tui.core.cli import main
from q3tui.core.ops import Ops
from q3tui.pipeline.base import StepContext, StepDef
from q3tui.pipeline.engine import Engine, EngineError
from q3tui.core.project import Project
from q3tui.steps import KINDS, register_kind


class NoteStep(StepDef):
    """A code-only step for flow tests: writes notes/<name>.txt from its option `text` and the previous note."""

    name = "note"
    title = "Note"

    def inputs(self, engine):
        return [engine.project.root / "notes" / f"{d}.txt" for d in self.deps]

    def outputs(self, engine):
        p = engine.project.root / "notes" / f"{self.name}.txt"
        return [p] if p.is_file() else []

    async def run(self, ctx: StepContext) -> None:
        d = ctx.project.root / "notes"
        d.mkdir(exist_ok=True)
        before = "".join((d / f"{x}.txt").read_text() for x in self.deps if (d / f"{x}.txt").is_file())
        (d / f"{self.name}.txt").write_text(before + str(self.options.get("text", self.name)) + "\n")


@pytest.fixture(autouse=True)
def _kind():
    register_kind("note", "tests.test_flows:NoteStep")
    yield
    KINDS.pop("note", None)


def write_flow(root, name="mine", **extra):
    (root / "flows").mkdir(exist_ok=True)
    data = {"name": name, "title": "a test flow", "steps": [
        {"id": "a", "kind": "note", "gate": "human", "options": {"text": "A"}},
        {"id": "b", "kind": "note", "deps": ["a"], "options": {"text": "B"}},
        {"id": "c", "kind": "note", "deps": ["b"], "gate": "auto"},
    ], **extra}
    (root / "flows" / f"{name}.yaml").write_text(yaml.safe_dump(data))


def engine_for(tmp_path, flow="mine", extra_cfg=""):
    (tmp_path / "q3tui.yaml").write_text(f"pipeline: {{flow: {flow}{extra_cfg}}}\n")
    return Engine(Project.open(tmp_path))


def run(engine, **kw):
    return anyio.run(lambda: engine.run(**kw))


def test_builtin_flows_are_valid():
    assert set(flows.list_flows()) == {"vlsit", "example", "fpga"}
    example = flows.load_flow("example")
    assert flows.validate(example) == [] and [s.id for s in example.steps] == ["spec", "parse"]


def test_vlsit_flow_is_valid():
    f = flows.load_flow("vlsit")
    assert flows.validate(f) == []
    assert f.gates["parse"] == "human" and f.gates["config"] == "auto" and f.gates["sva"] == "human" and f.gates["verify"] == "human"


def test_validation_problems(tmp_path):
    bad = flows.parse_flow({"name": "x", "steps": [{"id": "a", "kind": "nope"}, {"id": "a", "kind": "spec"},
                                                   {"id": "b", "kind": "spec", "deps": ["zzz"]}]})
    text = " | ".join(flows.validate(bad))
    assert "unknown kind 'nope'" in text and "more than once" in text and "unknown step 'zzz'" in text
    cyc = flows.parse_flow({"name": "x", "steps": [{"id": "a", "kind": "spec", "deps": ["b"]}, {"id": "b", "kind": "spec", "deps": ["a"]}]})
    assert "cycle" in " ".join(flows.validate(cyc))
    with pytest.raises(flows.FlowError):
        flows.parse_flow({"name": "x", "steps": [{"id": "a", "kind": "spec", "gate": "sometimes"}]})
    with pytest.raises(flows.FlowError):
        flows.parse_flow({"name": "x", "steps": []})
    with pytest.raises(flows.FlowError):
        flows.parse_flow({"name": "x", "steps": [{"id": "a", "kind": "spec", "colour": "red"}]})


def test_flow_files_are_found_project_before_user_before_builtin(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("Q3TUI_HOME", str(home))
    (home / "flows").mkdir(parents=True)
    (home / "flows" / "mine.yaml").write_text(yaml.safe_dump({"name": "mine", "title": "user", "steps": [{"id": "a", "kind": "note"}]}))
    (home / "flows" / "example.yaml").write_text(yaml.safe_dump({"name": "example", "title": "shadowed", "steps": [{"id": "a", "kind": "note"}]}))
    proj = tmp_path / "p"
    proj.mkdir()
    assert flows.load_flow("mine", proj).title == "user"
    write_flow(proj)
    assert flows.load_flow("mine", proj).title == "a test flow"  # the project's wins
    assert flows.load_flow("example", proj).title == "shadowed"  # a user flow shadows a built-in one
    assert set(flows.list_flows(proj)) >= {"mine", "example", "vlsit"}
    assert flows.load_flow("flows/mine.yaml", proj).name == "mine"  # by path
    with pytest.raises(flows.FlowError):
        flows.load_flow("does-not-exist", proj)


def test_steps_run_in_dependency_order_not_file_order(tmp_path):
    f = flows.parse_flow({"name": "x", "steps": [{"id": "c", "kind": "note", "deps": ["b"]}, {"id": "b", "kind": "note", "deps": ["a"]},
                                                 {"id": "a", "kind": "note"}]})
    assert [s.name for s in flows.build_steps(f)] == ["a", "b", "c"]


def test_a_custom_flow_runs_with_its_gates(tmp_path):
    write_flow(tmp_path)
    eng = engine_for(tmp_path)
    assert [v.name for v in eng.status()] == ["a", "b", "c"]
    assert eng.gate_modes() == {"a": "human", "c": "auto"}
    assert eng.label("b") == "2"  # no label in the file: its position
    assert run(eng) == "gate"  # a: human gate
    assert eng.evaluate(eng.by_name["a"]).gate == "open"
    eng.approve("a")
    assert run(eng) == "complete"  # c's gate is auto: it approved itself
    assert [p.read_text() for p in (tmp_path / "notes").glob("c.txt")] == ["A\nB\nc\n"]
    assert eng.evaluate(eng.by_name["c"]).gate == "approved"
    assert run(eng) == "complete"  # nothing to do: up to date


def test_gate_mode_changes_take_effect(tmp_path):
    write_flow(tmp_path)
    eng = engine_for(tmp_path)
    ops = Ops(eng)
    assert "auto" in ops.set_gate_mode("a", "auto")
    assert run(eng) == "complete"  # a approved itself
    ops.set_gate_mode("b", "human")
    eng.reset("b")
    assert run(eng) == "gate"
    assert "no review gate" in ops.set_gate_mode("b", "none")
    assert run(eng) == "complete"
    with pytest.raises(EngineError):
        ops.set_gate_mode("a", "sometimes")
    with pytest.raises(EngineError):
        ops.set_gate_mode("zzz", "auto")
    assert "gates" in ops.gates_text()


def test_gate_modes_from_the_config_override_the_flow(tmp_path):
    write_flow(tmp_path)
    eng = engine_for(tmp_path, extra_cfg=", gate_modes: {a: auto_answer, b: human}")
    assert eng.gate_modes() == {"a": "auto_answer", "b": "human", "c": "auto"}
    assert run(eng) == "gate"  # a and c sign themselves, b stops for you
    assert eng.evaluate(eng.by_name["a"]).gate == "approved" and eng.evaluate(eng.by_name["b"]).gate == "open"


def test_explicit_gates_replace_the_flows(tmp_path):
    write_flow(tmp_path)
    eng = engine_for(tmp_path, extra_cfg=", gates: [b]")
    assert eng.gate_modes() == {"b": "human"}
    assert run(eng) == "gate" and eng.evaluate(eng.by_name["b"]).gate == "open"


def test_project_wide_switches_still_apply(tmp_path):
    write_flow(tmp_path)
    eng = engine_for(tmp_path, extra_cfg=", auto_approve: true")
    assert run(eng) == "complete"


def test_changing_a_steps_options_makes_it_stale(tmp_path):
    write_flow(tmp_path)
    eng = engine_for(tmp_path, extra_cfg=", auto_approve: true")
    assert run(eng) == "complete"
    assert all(v.status == "done" for v in eng.status())
    write_flow(tmp_path, name="mine")
    data = yaml.safe_load((tmp_path / "flows" / "mine.yaml").read_text())
    data["steps"][1]["options"] = {"text": "B2"}
    (tmp_path / "flows" / "mine.yaml").write_text(yaml.safe_dump(data))
    eng = Engine(Project.open(tmp_path))
    status = {v.name: v.status for v in eng.status()}
    assert status["a"] == "done" and status["b"] == "stale" and status["c"] == "done"  # c follows once b's output changes
    assert run(eng) == "complete"
    assert (tmp_path / "notes" / "c.txt").read_text() == "A\nB2\nc\n"


def test_reset_follows_the_flows_dependencies(tmp_path):
    write_flow(tmp_path)
    eng = engine_for(tmp_path, extra_cfg=", auto_approve: true")
    run(eng)
    assert eng.downstream("b") == ["b", "c"]
    eng.reset("b")
    assert (tmp_path / "notes" / "a.txt").is_file() and not (tmp_path / "notes" / "b.txt").is_file()


def test_gap_targets_and_sees_come_from_the_flow(tmp_path):
    write_flow(tmp_path, gap_targets={"spec_gap": "a"}, sees={"c": ["a"]})
    eng = engine_for(tmp_path)
    assert eng.gap_target("c", {"kind": "spec_gap"}) == "a"
    assert eng.gap_target("a", {"kind": "spec_gap"}) is None  # never a later or the same step


def test_cli_flow_commands(tmp_path):
    write_flow(tmp_path)
    (tmp_path / "q3tui.yaml").write_text("pipeline: {flow: mine}\n")
    r = CliRunner().invoke(main, ["-C", str(tmp_path), "flow", "list"])
    assert r.exit_code == 0 and "mine" in r.output and "example" in r.output
    r = CliRunner().invoke(main, ["-C", str(tmp_path), "flow", "check"])
    assert r.exit_code == 0 and "flow is valid" in r.output
    r = CliRunner().invoke(main, ["-C", str(tmp_path), "flow", "check", "vlsit"])
    assert r.exit_code == 0 and "tools needed: lint, synth, sim, run" in r.output
    r = CliRunner().invoke(main, ["-C", str(tmp_path), "gate", "a", "auto"])
    assert r.exit_code == 0 and "auto" in r.output
    assert "a: auto" in (tmp_path / "q3tui.yaml").read_text().replace("'", "")
    r = CliRunner().invoke(main, ["-C", str(tmp_path), "--flow", "vlsit", "status"])
    assert r.exit_code == 0 and "parse" in r.output


def test_a_gap_is_never_handed_to_a_step_whose_output_is_yours(tmp_path):
    """A hand-written spec (origin "user") cannot be rewritten for a decision: the answer stays with the step that asked
    (a real run: tb's spec_gap queued a change for the spec, and the run stopped with "no spec and no intent")."""
    from q3tui.pipeline.state import StepRecord

    write_flow(tmp_path, gap_targets={"spec_gap": "a"})
    eng = engine_for(tmp_path)
    assert eng.gap_target("c", {"kind": "spec_gap"}) == "a"
    eng.state.steps["a"] = StepRecord(status="done", origin="user")
    assert eng.gap_target("c", {"kind": "spec_gap"}) is None


def test_queued_decisions_for_a_step_you_provided_are_dropped_on_load(tmp_path):
    """State written before gaps were refused: a queued decision made the hand-written spec "stale", and the run stopped
    with "no spec and no intent"."""
    from q3tui.pipeline.engine import DECISION_PREFIX
    from q3tui.pipeline.state import StepRecord

    write_flow(tmp_path)
    eng = engine_for(tmp_path)
    eng.state.steps["a"] = StepRecord(status="done", origin="user")
    eng.state.feedback["a"] = [f"{DECISION_PREFIX} from c: use i_clk", "my own change request"]
    eng.save()
    eng2 = Engine(Project.open(tmp_path))
    assert eng2.state.feedback["a"] == ["my own change request"]  # only your own request stays
    eng2.push_decision("a", "c", "another one", rebase=False)  # and none is queued any more
    assert eng2.state.feedback["a"] == ["my own change request"]


def test_vlsit_is_the_default_flow(tmp_path):
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    (fresh / "q3tui.yaml").write_text("llm: {model: claude-haiku-4-5}\n")
    p = Project.open(fresh)
    assert p.cfg.pipeline.flow == "vlsit" and p.flow_ref() == "vlsit"
    assert [s.name for s in Engine(p).pipeline_steps][:3] == ["spec", "parse", "config"]


MD_FLOW = """---
name: mdflow
title: A markdown flow
---
Two notes, the second must contain "b".

## a
- kind: note
- gate: auto
- options: {text: a}
- view: notes/*.txt
- pass: exists notes/a.txt

Keep it short.

## b
- kind: note
- deps: [a]
- options: {text: b}
- pass: contains notes/b.txt ^zzz$
"""


def test_markdown_flow_has_view_pass_and_notes(tmp_path):
    (tmp_path / "flows").mkdir()
    (tmp_path / "flows" / "mdflow.md").write_text(MD_FLOW)
    flow = flows.load_flow("mdflow", tmp_path)
    assert [s.id for s in flow.steps] == ["a", "b"] and flow.steps[0].gate == "auto" and "second must" in flow.description
    a, b = flow.steps
    assert a.options == {"text": "a"} and a.view == ["notes/*.txt"] and a.passes == ["exists notes/a.txt"]
    assert a.notes == "Keep it short." and b.deps == ["a"]
    assert flows.check_pass(a.passes, tmp_path) == ["exists notes/a.txt"]  # nothing written yet
    assert flows.check_pass(["bogus x"], tmp_path)  # an unknown condition never passes

    (tmp_path / "q3tui.yaml").write_text("pipeline: {flow: mdflow}\n")
    eng = Engine(Project.open(tmp_path))
    assert eng.flow.name == "mdflow"
    async def run():
        await eng.run_step(eng.by_name["a"])  # passes: a.txt exists
        assert eng.state.steps["a"].status == "done"
        assert [p.name for p in eng.view_files("a")] == ["a.txt"]
        with pytest.raises(Exception, match="pass condition not met"):
            await eng.run_step(eng.by_name["b"])
        assert eng.state.steps["b"].status == "failed"

    anyio.run(run)


def test_skill_sections_are_the_prompts_and_a_flow_copy_overrides_them(tmp_path):
    from pathlib import Path

    from q3tui.llm import prompts
    from q3tui.llm.runtime import Stage

    builtin = flows.BUILTIN_DIR / "vlsit"
    base = flows.skill_sections(builtin / "parse" / "md" / "skill.md")
    assert "SYSTEM" in base and flows.load_flow("vlsit").steps[2].notes == ""  # sections are not notes
    copy = tmp_path / "flows" / "vlsit"
    for p in builtin.glob("*/md/skill.md"):
        (copy / p.parent.parent.name / "md").mkdir(parents=True, exist_ok=True)
        (copy / p.parent.parent.name / "md" / "skill.md").write_text(p.read_text().replace(base["SYSTEM"], "MY OWN SYSTEM PROMPT") if p.parent.parent.name == "parse" else p.read_text())
    (copy / "FLOW.md").write_text((builtin / "FLOW.md").read_text())
    st = Stage(name="parse_spec", system_prompt=base["SYSTEM"], prompt="p", cwd=tmp_path)
    prompts.apply(st, tmp_path, "parse", tmp_path / ".q3tui", copy, builtin)
    assert st.system_prompt == "MY OWN SYSTEM PROMPT"
