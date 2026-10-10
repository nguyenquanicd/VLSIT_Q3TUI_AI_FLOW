"""Add-ons: a flow folder's addon.py / tools.json / panels.py reach the one Q3TUI (CLI, Ops, assistant, TUI) with no flow-specific
code in core — shown with a throwaway flow that has nothing to do with FPGA."""

import json

import anyio
import pytest
from click.testing import CliRunner

from q3tui import flows
from q3tui.core.cli import main
from q3tui.core.ops import Ops
from q3tui.core.project import Project
from q3tui.llm.assistant import assistant_tools
from q3tui.pipeline.engine import Engine, EngineError

ADDON = '''
def register(api):
    @api.action("greet", "Say hello to someone", {"who": "a name", "punct": "how to end it"}, optional=("punct",))
    def greet(ops, who, punct="!"):
        if who == "bad":
            raise ValueError("no bad people")
        return f"hello {who}{punct} ({len(ops.engine.steps)} steps)"
'''
PANELS = "from dataclasses import dataclass\n\n\n@dataclass\nclass Row:  # (a dataclass needs its module registered by name)\n    x: int = 1\n\n\nPANELS_IMPORTED = True\n"


@pytest.fixture
def proj(tmp_path):
    f = tmp_path / "flows" / "greeter"
    (f / "one" / "md").mkdir(parents=True)
    (f / "FLOW.md").write_text("---\nname: greeter\ntools: [say]\nsteps: [one]\n---\nA flow with an add-on.\n")
    (f / "one" / "md" / "skill.md").write_text("---\nkind: spec\n---\n")
    (f / "addon.py").write_text(ADDON)
    (f / "panels.py").write_text(PANELS)
    (f / "tools.json").write_text(json.dumps({"roles": {"say": {"cmd": "echo hi"}}}))
    (tmp_path / "q3tui.yaml").write_text("pipeline: {flow: greeter}\n")
    return Project.open(tmp_path)


class Host:
    def __init__(self, engine):
        self.engine, self.ops = engine, Ops(engine)

    async def confirm(self, question, detail=""):
        return True


def test_the_flow_brings_its_files(proj):
    flow = flows.load_flow("greeter", proj.root)
    assert list(flow.actions) == ["greet"] and flow.tool_defaults["say"]["cmd"] == "echo hi" and flow.panels_file.name == "panels.py"
    assert flows.load_flow("vlsit").actions == {} and flows.load_flow("vlsit").tool_defaults == {}  # other flows: nothing


def test_ops_runs_actions(proj):
    ops = Ops(Engine(proj))
    assert ops.run_action("greet", who="you") == "hello you! (1 steps)"
    assert ops.run_action("greet", who="you", punct="?") == "hello you? (1 steps)"
    with pytest.raises(EngineError, match="needs who"):
        ops.run_action("greet")
    with pytest.raises(EngineError, match="no parameter whom"):
        ops.run_action("greet", who="x", whom="y")
    with pytest.raises(EngineError, match="no bad people"):  # a ValueError of the action is a user error, not a crash
        ops.run_action("greet", who="bad")
    with pytest.raises(EngineError, match="no action 'nope' .*greet"):
        ops.run_action("nope")


def test_the_assistant_gets_a_tool_per_action(proj):
    tools = assistant_tools(Host(Engine(proj)))
    t = next(t for t in tools if t.name == "greet")
    assert t.input_schema["required"] == ["who"] and set(t.input_schema["properties"]) == {"who", "punct"}
    out = anyio.run(lambda: t.handler({"who": "world"}))
    assert out["content"][0]["text"] == "hello world! (1 steps)"
    assert not any(x.name == "greet" for x in assistant_tools(Host(Engine(Project.open(proj.root, overrides={"pipeline": {"flow": "vlsit"}})))))


def test_cli_act(proj):
    r = CliRunner().invoke(main, ["-C", str(proj.root), "act"])
    assert r.exit_code == 0 and "greet" in r.output and "Say hello" in r.output
    r = CliRunner().invoke(main, ["-C", str(proj.root), "act", "greet", "who=Ada", "punct=."])
    assert r.exit_code == 0 and "hello Ada. (1 steps)" in r.output
    r = CliRunner().invoke(main, ["-C", str(proj.root), "act", "greet", "Ada"])
    assert r.exit_code != 0 and "key=value" in r.output


def test_tools_check_uses_the_flows_default_role(proj):
    r = CliRunner().invoke(main, ["-C", str(proj.root), "tools", "check"])
    assert "say" in r.output and "not configured" not in r.output


async def test_tui_slash_act_and_panels(proj):
    from q3tui.tui.app import Q3TUIApp

    app = Q3TUIApp(proj)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert getattr(app.engine.flow, "_panels_loaded", False)  # the flow's panels.py was imported
        assert app._completions("action") == ["greet"]
        from textual.widgets import Input

        said: list[str] = []
        app.log_info = said.append  # what the command reports
        box = app.query_one("#cmd", Input)
        box.value = "/act greet who=Zed"
        await box.action_submit()
        await pilot.pause()
        assert said == ["hello Zed! (1 steps)"]
