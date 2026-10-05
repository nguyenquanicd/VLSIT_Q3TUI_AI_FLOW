"""Every user action is available as an operation and as an assistant tool."""

import anyio
import pytest

from q3tui.llm.assistant import assistant_tools
from q3tui.llm import runtime
from q3tui.core.ops import Ops
from q3tui.pipeline.engine import Engine, EngineError
from q3tui.core.project import Project
from q3tui.steps.spec.sections import current_sections, section_states
from tests.fakes import FakeLLM, use_test_flow


@pytest.fixture
def llm(monkeypatch):
    fake = FakeLLM()
    monkeypatch.setattr(runtime, "run_stage", fake)
    return fake


@pytest.fixture
def engine(tmp_path, llm):
    use_test_flow(tmp_path, "spec: {self_review: false}\n")
    e = Engine(Project.open(tmp_path))
    e.import_intent("A two-stage FIFO")
    anyio.run(lambda: e.run(only="spec"))
    return e


class Host:
    def __init__(self, engine, confirm=True, focus=None):
        self.engine, self.ops = engine, Ops(engine)
        self.answer = confirm
        self.focus = focus
        self.runs, self.asked = [], []

    def focus_step(self):
        return self.focus

    def start_run(self, start=None, stop=None, only=None, regenerate=None):
        self.runs.append((start, stop, only, regenerate))
        return "run started"

    async def run_and_wait(self, start=None, stop=None, only=None, regenerate=None):
        self.runs.append((start, stop, only, regenerate, "wait"))
        return "run finished: gate"

    def stop_run(self):
        return "nothing is running"

    async def confirm(self, question, detail=""):
        self.asked.append(question)
        return self.answer


def call(tools, name, **args):
    t = next(t for t in tools if t.name == name)
    out = anyio.run(lambda: t.handler(args))
    return out["content"][0]["text"], out.get("is_error", False)


def test_create_section_and_write_it_by_prompt(engine):
    """"Add a Power Management section after Performance and write it" — two tool calls."""
    host = Host(engine)
    tools = assistant_tools(host)
    text, err = call(tools, "add_section", title="Power Management", guidance="Clock gating, power states",
                     fields=[{"name": "Power states"}], after="performance")
    assert not err and "power_management" in text
    ids = [s.id for s in host.ops.template().sections]
    assert ids.index("power_management") == ids.index("performance") + 1
    assert engine.evaluate(engine.by_name["spec"]).status == "stale"  # template is a spec input

    text, err = call(tools, "write_section", section="power_management",
                     content="The block shall gate `clk` when idle.", fields={"Power states": "ACTIVE, IDLE"})
    assert not err
    written = current_sections(engine)
    assert list(written).index("power_management") == list(written).index("performance") + 1
    assert "ACTIVE, IDLE" in written["power_management"][1]
    st = {s.id: s for s in section_states(engine)}["power_management"]
    assert st.in_spec and st.edited


def test_template_ops(engine):
    ops = Ops(engine)
    ops.update_section("Overview", required=False, guidance="short")
    ops.move_section("overview", after="features")
    ops.remove_section("verification")
    t = ops.template()
    assert [s.id for s in t.sections][:2] == ["features", "overview"]
    assert not t.by_id()["overview"].required and "verification" not in t.by_id()
    with pytest.raises(EngineError):
        ops.remove_section("nope")


def test_review_change_intent_model_cost(engine):
    host = Host(engine)
    tools = assistant_tools(host)
    assert not call(tools, "mark_reviewed", section="overview")[1]
    assert {s.id: s.reviewed for s in section_states(engine)}["overview"]
    call(tools, "request_change", step="spec", text="mention reset", section="overview")
    assert engine.state.feedback["spec"] == ["In section 'Overview': mention reset"]
    call(tools, "append_intent", text="Also: 16-bit counter.")
    assert "16-bit counter" in (engine.project.spec_dir / "intent.md").read_text()
    call(tools, "set_model", model="claude-sonnet-5-5", effort="medium")
    assert (engine.project.cfg.llm.model, engine.project.cfg.llm.effort) == ("claude-sonnet-5-5", "medium")
    call(tools, "run_steps", only="spec", regenerate=["spec"])
    assert host.runs == [(None, None, "spec", {"spec"})]
    assert "$" in call(tools, "show_cost")[0]


def test_destructive_actions_ask_the_user(engine):
    host = Host(engine, confirm=False)
    tools = assistant_tools(host)
    assert call(tools, "reset_step", step="spec")[0] == "the user declined"
    assert (engine.project.spec_dir / "spec.md").exists()
    assert call(tools, "reset_cost")[0] == "the user declined"
    host.answer = True
    text, err = call(tools, "reset_step", step="spec")
    assert not err and "removed" in text and not (engine.project.spec_dir / "spec.md").exists()
    assert len(host.asked) == 3


def test_errors_are_reported_to_the_model(engine):
    tools = assistant_tools(Host(engine))
    text, err = call(tools, "remove_section", section="does_not_exist")
    assert err and "no section" in text


def test_assistant_can_write_spec_and_model_only(tmp_path):
    from q3tui.llm.runtime import Stage, check_tool_paths

    st = Stage(name="a", system_prompt="", prompt="", cwd=tmp_path, deny_dirs=[tmp_path / ".q3tui"],
               write_dirs=[tmp_path / "spec", tmp_path / "req", tmp_path / "arch"])
    assert check_tool_paths(st, {"file_path": "spec/spec.md"}, "Edit") is None
    assert check_tool_paths(st, {"file_path": "rtl/top.sv"}, "Read") is None  # may read RTL
    assert "may not be modified" in check_tool_paths(st, {"file_path": "rtl/top.sv"}, "Write")
    assert "not accessible" in check_tool_paths(st, {"file_path": ".q3tui/pipeline.json"}, "Read")


def test_set_section_body(engine):
    ops = Ops(engine)
    ops.set_section_body("overview", "The block buffers **words**.\n\n### Notes\n- two stages")
    assert current_sections(engine)["overview"][1].startswith("The block buffers **words**.")
    assert {s.id: s for s in section_states(engine)}["overview"].edited



def test_run_steps_can_wait(engine):
    host = Host(engine)
    tools = assistant_tools(host)
    assert call(tools, "run_steps", only="spec", wait=True)[0] == "run finished: gate"
    assert host.runs[-1][-1] == "wait"



def test_assistant_stays_in_the_users_step(engine):
    """In the requirements view: spec hand-off is allowed, touching other steps needs the user's OK."""
    host = Host(engine, confirm=False, focus="req")
    tools = assistant_tools(host)

    # hand-off to the spec: allowed without asking
    text, err = call(tools, "request_change", step="spec", text="state the 100 MHz minimum")
    assert not err and "queued change for spec" in text and host.asked == []
    assert call(tools, "run_steps", only="spec", wait=True)[0] == "run finished: gate" and host.asked == []

    # other steps: asked, and declined here
    for tool_name, args in (("approve", {"step": "spec"}), ("write_section", {"section": "overview", "content": "x"}),
                            ("add_section", {"title": "Power"}), ("run_steps", {})):
        text, _ = call(tools, tool_name, **args)
        assert text.startswith("the user declined: stay on step 'req'"), tool_name
    assert len(host.asked) == 4 and "while you are working on 'req'" in host.asked[0]

    # its own step: no question
    host.asked.clear()
    call(tools, "run_steps", only="req")
    assert host.asked == []


def test_settings_are_validated_applied_and_saved(engine):
    ops = Ops(engine)
    cfg = engine.project.cfg
    assert ops.settings()["rtl.parallel"] == 4 and ops.settings()["pipeline.gates"] == []
    msg = ops.apply_settings({"tb.max_turns": "40", "spec.self_review": "off", "pipeline.gates": "spec, parse"})
    assert cfg.tb.max_turns == 40 and cfg.spec.self_review is False and cfg.pipeline.gates == ["spec", "parse"]
    assert "tb.max_turns = 40" in msg and not (engine.project.root / "q3tui.yaml").read_text().count("max_turns")
    assert ops.apply_settings({"tb.max_turns": 40}) == "settings unchanged"
    ops.apply_settings({"llm.timeout_s": ""})           # empty: no limit (like Claude Code)
    assert cfg.llm.timeout_s is None
    with pytest.raises(EngineError, match="rtl.parallel"):
        ops.apply_settings({"rtl.parallel": 0})               # schema: ge=1 — nothing applied
    assert cfg.rtl.parallel == 4
    with pytest.raises(EngineError, match="unknown setting"):
        ops.apply_settings({"llm.colour": "blue"})
    with pytest.raises(EngineError, match="unknown vplan2"):
        ops.apply_settings({"pipeline.gates": ["vplan2"]})
    ops.apply_settings({"llm.model": "claude-haiku-4-5", "llm.thinking_budget": ""}, save=True)
    assert cfg.llm.effort is None and cfg.llm.thinking_budget is None   # Haiku: no effort; empty = model default
    saved = (engine.project.root / "q3tui.yaml").read_text()
    assert "claude-haiku-4-5" in saved and "thinking_budget: null" in saved


def test_assistant_settings_tools(engine):
    host = Host(engine)
    tools = assistant_tools(host)
    text, err = call(tools, "get_settings")
    assert not err and "llm.effort" in text and "rtl.max_turns" in text and "\nSpec:\n" in text
    text, err = call(tools, "set_settings", changes={"rtl.max_fix_attempts": 1})
    assert not err and engine.project.cfg.rtl.max_fix_attempts == 1
    host.answer = False
    text, _ = call(tools, "set_settings", changes={"pipeline.auto_approve": True})
    assert "declined" in text and engine.project.cfg.pipeline.auto_approve is False


# -- gates, asking the user, VLSIT review (the picker's tools) ------------------------------------------------------


def test_gate_mode_ops_and_tools(engine):
    host = Host(engine)
    tools = assistant_tools(host)
    text, err = call(tools, "gates")
    assert not err and "spec" in text and "human" in text
    text, err = call(tools, "set_gate_mode", step="spec", mode="auto")
    assert not err and engine.gate_mode("spec") == "auto" and host.asked            # turning a gate to auto asks
    host.answer = False
    text, err = call(tools, "set_gate_mode", step="spec", mode="auto_answer")
    assert "declined" in text and engine.gate_mode("spec") == "auto"
    _, err = call(tools, "set_gate_mode", step="spec", mode="sometimes")
    assert err
    ops = Ops(engine)
    assert "no review gate" in ops.set_gate_mode("spec", "none")
    assert engine.gate_mode("spec") is None and "spec" not in engine.gate_modes()
    assert "spec" in ops.gates_text()


def test_ask_user_tool(engine):
    host = Host(engine)

    async def ask_user(questions):
        host.asked.append(questions)
        return [f"answer {i}" for i, _ in enumerate(questions)]

    host.ask_user = ask_user
    tools = assistant_tools(host)
    text, err = call(tools, "ask_user", questions=[{"question": "Which FIFO depth?", "header": "Depth",
                                                    "options": [{"label": "16", "description": "small"}, {"label": "32"}]}])
    assert not err and "Depth: answer 0" in text and host.asked[-1][0]["options"][0]["label"] == "16"
    _, err = call(tools, "ask_user", questions=[])
    assert err


def test_vlsit_review_ops_without_the_review_module(engine, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "q3tui.flows.vlsit.sva.review", None)   # (import fails → a friendly message)
    tools = assistant_tools(Host(engine))
    for name, args in (("list_properties", {}), ("rtm_status", {}), ("review_property", {"label": "a_x", "status": "confirmed"}),
                       ("sign_requirement", {"req_id": "REQ-001"})):
        text, err = call(tools, name, **args)
        assert err and "review module" in text


def test_vlsit_review_ops_call_the_review_module(engine, monkeypatch):
    import sys
    import types

    calls = []
    fake = types.ModuleType("q3tui.flows.vlsit.sva.review")
    fake.load_properties = lambda eng: [{"label": "a_x", "status": "pending", "req_ids": ["REQ-001"], "nl": "x is never X"}]
    fake.review_property = lambda eng, label, status, note="": calls.append(("prop", label, status, note))
    fake.rtm_rows = lambda eng: [{"req_id": "REQ-001", "signed_off": False}]
    fake.sign_req = lambda eng, rid, signed, note="": calls.append(("sign", rid, signed, note)) or f"{rid} signed"
    monkeypatch.setitem(sys.modules, "q3tui.flows.vlsit.sva.review", fake)
    ops = Ops(engine)
    assert ops.properties()[0]["label"] == "a_x" and ops.rtm()[0]["req_id"] == "REQ-001"
    assert "rejected" in ops.review_property("a_x", "rejected", "too weak")
    assert ops.sign_req("REQ-001", True, "checked") == "REQ-001 signed"
    assert calls == [("prop", "a_x", "rejected", "too weak"), ("sign", "REQ-001", True, "checked")]
    with pytest.raises(EngineError):
        ops.review_property("a_x", "maybe")
