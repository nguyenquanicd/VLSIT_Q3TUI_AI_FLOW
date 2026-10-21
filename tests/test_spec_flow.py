"""flows/spec/ end to end through Engine.run: spec → parse → config, with the human gate and fake LLM stages (the handlers of
tests/test_spec_parse_config.py). No live LLM."""

import json

import anyio
import pytest
from click.testing import CliRunner

from q3tui.core.cli import main
from q3tui.core.project import Project
from q3tui.flows.spec.lib import artifacts
from q3tui.llm import runtime
from q3tui.pipeline.engine import Engine
from tests import vlsit_fakes
from tests.fakes import FakeLLM
from tests.test_spec_parse_config import VlsitFake


def parsed_spec(req2="b is registered") -> dict:
    return {
        "ip_name": "demo", "spec_revision": "0.1", "top_module": "m_top", "description": "demo",
        "requirements": [
            {"id": "REQ-001", "text": "a is registered", "category": "functional", "rtl_modules": ["m_leaf_a"], "sva_hint": "property"},
            {"id": "REQ-002", "text": req2, "category": "functional", "rtl_modules": ["m_leaf_b"], "sva_hint": "property"},
            {"id": "REQ-003", "text": "top ties them", "category": "interface", "rtl_modules": ["m_top"], "sva_hint": "property"}],
        "parameters": [{"name": "PARA_W", "type": "int unsigned", "default": "8", "valid_range": "8..64", "description": "width"}],
        "constraints": [{"id": "C1", "rule": "PARA_W % 8 == 0", "description": "byte aligned"}],
        "modules": [{"name": "m_top", "description": "top", "instances": ["m_leaf_a", "m_leaf_b"]},
                    {"name": "m_leaf_a", "description": "a"}, {"name": "m_leaf_b", "description": "b"}],
    }


@pytest.fixture
def llm(monkeypatch):
    fake = FakeLLM()
    vf = VlsitFake()
    vf.parsed = [parsed_spec()]
    fake.vf = vf
    saved = list(vlsit_fakes.HANDLERS)
    vlsit_fakes.HANDLERS[:] = [vf]
    monkeypatch.setattr(runtime, "run_stage", fake)
    yield fake
    vlsit_fakes.HANDLERS[:] = saved


def make_engine(tmp_path, cfg: str = "") -> Engine:
    (tmp_path / "q3tui.yaml").write_text("pipeline:\n  flow: spec\n" + cfg)
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec" / "demo_spec.md").write_text("# demo spec\n\nF01 a is registered.\n")
    return Engine(Project.open(tmp_path))


def run(eng, **kw):
    return anyio.run(lambda: eng.run(**kw))


def names(llm):
    return [c.name for c in llm.calls]


def errors(eng):
    return {k: r.error for k, r in eng.state.steps.items() if r.error}


def drive(eng):
    """Run to the end like a user would: approve every gate."""
    for _ in range(6):
        out = run(eng)
        if out not in ("gate", "complete"):
            raise AssertionError(f"{out}: {errors(eng)}")
        if out != "gate":
            return out
        for name in eng.pending_approvals():
            eng.approve(name)
    raise AssertionError("the flow keeps stopping at gates")


def statuses(eng):
    return {v.name: (v.status, v.gate) for v in eng.status()}


def test_the_spec_flow_end_to_end(tmp_path, llm):
    eng = make_engine(tmp_path)
    assert [s.name for s in eng.pipeline_steps] == ["spec", "parse", "config"]
    assert eng.gate_modes() == {"spec": "human", "parse": "human", "config": "auto"}
    p = eng.project

    # Gate 1 (human): parse stops the run
    assert run(eng) == "gate", errors(eng)
    assert statuses(eng)["parse"] == ("done", "open") and statuses(eng)["config"][0] == "pending"
    spec = artifacts.read(p, "structured_spec.json")
    assert artifacts.validate("structured_spec.json", spec) == []
    assert spec["metadata"]["gate_status"] == "pending"
    assert [r["req_id"] for r in spec["requirements"]] == ["REQ-001", "REQ-002", "REQ-003"]
    eng.approve("parse")
    assert artifacts.read(p, "structured_spec.json")["metadata"]["gate_status"] == "approved"
    assert eng.evaluate(eng.by_name["parse"]).edited == [] and statuses(eng)["parse"] == ("done", "approved")

    # config (Gate 2 auto) signs itself
    assert run(eng) == "complete", errors(eng)
    assert statuses(eng)["config"] == ("done", "approved")
    cfg = artifacts.read(p, "final_config.json")
    assert artifacts.validate("final_config.json", cfg) == [] and cfg["metadata"]["gate_2_approved"] is True

    # nothing went stale behind the gate approvals, and a further run does nothing
    before = len(llm.calls)
    assert run(eng) == "complete" and len(llm.calls) == before
    assert {k: s for k, s in statuses(eng).items() if s[0] not in ("done", "user") or s[1] == "open"} == {}
    assert {s.name: eng.evaluate(s).edited for s in eng.steps if eng.evaluate(s).edited} == {}


def test_a_changed_requirement_updates_only_what_it_touches(tmp_path, llm):
    eng = make_engine(tmp_path)
    assert drive(eng) == "complete"
    llm.calls.clear()
    llm.vf.parsed = [parsed_spec(req2="b is registered and cleared by reset")]
    eng.request_change("parse", "REQ-002 also needs the reset clearing")
    assert run(eng) == "gate"  # Gate 1 again
    eng.approve("parse")
    assert drive(eng) == "complete"
    assert "parse_spec" in names(llm) and "spec_write" not in names(llm)
    assert {k: v for k, v in statuses(eng).items() if v[0] not in ("done", "user")} == {}


def test_reset_follows_the_flows_dependencies(tmp_path, llm):
    eng = make_engine(tmp_path)
    assert drive(eng) == "complete"
    assert eng.downstream("parse") == ["parse", "config"]
    assert eng.downstream("config") == ["config"]
    eng.reset("config")
    p = eng.project
    assert (p.schemas_dir / "structured_spec.json").is_file() and not (p.schemas_dir / "final_config.json").is_file()
    st = statuses(eng)
    assert st["parse"][0] == "done" and st["config"][0] == "pending"


def test_gate_modes_decide_where_the_run_stops(tmp_path, llm):
    eng = make_engine(tmp_path, "  gate_modes: {parse: auto}\n")
    assert run(eng) == "complete"  # no human gate is left: parse signs itself
    assert statuses(eng)["parse"] == ("done", "approved")


def test_flow_check_lists_the_spec_flow(tmp_path):
    (tmp_path / "q3tui.yaml").write_text("pipeline: {flow: spec}\n")
    r = CliRunner().invoke(main, ["-C", str(tmp_path), "flow", "check"])
    assert r.exit_code == 0, r.output
    assert "vlsit_parse" in r.output and "flow is valid" in r.output
    r = CliRunner().invoke(main, ["-C", str(tmp_path), "gate", "parse", "auto"])
    assert r.exit_code == 0 and "parse: auto" in (tmp_path / "q3tui.yaml").read_text().replace("'", "")


FLOW_SESSION = """name: t
session: flow
fresh_steps: [spec]
steps:
  - {id: spec, kind: spec, gate: none}
  - {id: parse, kind: vlsit_parse, gate: none}
  - {id: config, kind: vlsit_config, gate: none}
"""


def make_session_engine(tmp_path, cfg: str = "") -> Engine:
    (tmp_path / "flows").mkdir()
    (tmp_path / "flows" / "t.yaml").write_text(FLOW_SESSION)
    (tmp_path / "q3tui.yaml").write_text("pipeline:\n  flow: t\n" + cfg)
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec" / "demo_spec.md").write_text("# demo spec\n\nF01 a is registered.\n")
    return Engine(Project.open(tmp_path))


def test_a_flow_with_session_flow_is_one_session(tmp_path, llm):
    """Like one Claude Code conversation: every LLM task of a step that is not in the flow file's `fresh_steps` goes to the
    one `flow` session; the fresh steps and reviewers run in sessions of their own."""
    from q3tui.llm import roles

    eng = make_session_engine(tmp_path)
    assert eng.session_mode() == "flow"
    drive(eng)
    tasks = [c for c in llm.calls if c.role]
    assert tasks and all(c.role is None for c in llm.calls if c.name.startswith("spec_") or "review" in c.name)
    assert {c.role for c in tasks} == {"flow"} and all(c.system_prompt == roles.CHARTERS["flow"] for c in tasks)  # one stable charter
    assert "# Project brief" in tasks[0].prompt and tasks[0].resume is None
    assert roles.notes_path(eng.project, "flow").name == "NOTES.md"


def test_session_can_be_set_back_to_fresh_per_stage(tmp_path, llm):
    eng = make_session_engine(tmp_path, cfg="  session: fresh\n")
    assert eng.session_mode() == "fresh"
    run(eng)
    assert llm.calls and all(c.role is None for c in llm.calls)


def test_the_example_flow_uses_fresh_sessions(tmp_path):
    (tmp_path / "q3tui.yaml").write_text("pipeline: {flow: example}\n")
    assert Engine(Project.open(tmp_path)).session_mode() == "fresh"


def test_a_full_reset_starts_the_flow_session_over(tmp_path, llm):
    from q3tui.llm import roles

    eng = make_session_engine(tmp_path)
    drive(eng)
    assert roles.team(eng.project).roles["flow"].tasks > 0
    eng.reset("spec")  # everything: the session starts over too
    state = json.loads((tmp_path / ".q3tui" / "team" / "roles.json").read_text())["flow"]
    assert state["session"] is None and state["tasks"] == 0 and state["sent"] == []
    assert not roles.notes_path(eng.project, "flow").is_file()
