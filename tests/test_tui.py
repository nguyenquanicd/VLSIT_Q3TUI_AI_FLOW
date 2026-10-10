"""Headless TUI tests (Textual pilot) with the LLM stubbed."""

import pytest

from q3tui.llm import runtime
from q3tui.core.project import Project
from q3tui.tui.app import Q3TUIApp
from q3tui.tui.picker import ChoiceScreen
from textual.screen import ModalScreen
from q3tui.tui.screens import GateScreen, TextPromptScreen
from tests.fakes import FakeLLM, use_test_flow


@pytest.fixture
def llm(monkeypatch):
    fake = FakeLLM()
    monkeypatch.setattr(runtime, "run_stage", fake)
    return fake


@pytest.fixture
def app(tmp_path):
    use_test_flow(tmp_path)
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec" / "intent.md").write_text("A two-stage FIFO\n")
    return Q3TUIApp(Project.open(tmp_path))


async def settle(app, pilot):
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()
    if app._open_gate() and not isinstance(app.screen, ModalScreen):  # (the review dialog opens on `a`, not by itself)
        app.action_review()
        await pilot.pause()


async def open_gate(app, pilot):
    """The review dialog no longer pops up by itself: `a` opens it."""
    if not isinstance(app.screen, GateScreen):
        await pilot.press("a")
        await pilot.pause()


async def test_intent_to_spec_in_tui(app, llm):
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.press("r")
        await settle(app, pilot)
        await open_gate(app, pilot)
        assert isinstance(app.screen, GateScreen) and app.screen.step == "spec"
        assert llm.names() == ["spec_write", "spec_review"]
        await pilot.press("l")  # later
        await pilot.pause()
        assert app.selected_step == "spec"  # lower panel followed the running step

        # Answer the spec question from its Questions tab.
        await pilot.press("o")
        await pilot.pause()
        questions = app.query_one("#view-spec QuestionsTable DataTable")
        assert questions.row_count == 1
        questions.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ChoiceScreen)             # the picker: suggested answers, then Other…
        await pilot.press(str(len(app.screen.choices) + 1))     # Other… → type it
        await pilot.pause()
        assert isinstance(app.screen, TextPromptScreen)
        app.screen.query_one("TextArea").text = "saturate at 255\nnever wrap"   # multi-line answer
        await pilot.press("ctrl+s")
        await pilot.pause()
        views = {v.name: v for v in app.engine.status()}
        assert views["spec"].status == "stale"


async def test_slash_commands_and_assistant(app, llm):
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.press("slash")
        assert app.focused.id == "cmd" and app.query_one("#cmd").value == "/"
        app.query_one("#cmd").value = "/status"
        await pilot.press("enter")
        await pilot.pause()
        lines = [str(line.text) for line in app.query_one("#activity").lines]
        assert any("spec" in l and "pending" in l for l in lines)

        app.query_one("#cmd").value = "what does REQ-001 mean?"
        await pilot.press("enter")
        await settle(app, pilot)
        call = llm.calls[-1]
        assert call.name == "assistant" and call.prompt.endswith("what does REQ-001 mean?")
        assert call.prompt.startswith("[TUI context] viewing step 1 spec (Specification)")  # knows what is on screen
        assert {t.name for t in llm.calls[-1].sdk_tools} >= {"pipeline_status", "request_change", "answer_question"}
        assert (app.project.state_dir / "tui" / "session.json").read_text().count("sess-1") == 1


async def test_reject_with_feedback(app, llm):
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.press("r")
        await settle(app, pilot)
        await pilot.press("r")  # request changes in the gate
        await pilot.pause()
        assert isinstance(app.screen, TextPromptScreen)
        app.screen.query_one("TextArea").text = "add a flush port"
        await pilot.press("ctrl+s")
        await settle(app, pilot)
        assert "add a flush port" in llm.calls[2].prompt
        await open_gate(app, pilot)
        assert isinstance(app.screen, GateScreen) and app.screen.step == "spec"


async def test_change_model(app, llm):
    from q3tui.tui.screens import ModelScreen

    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.press("slash")
        app.query_one("#cmd").value = "/model claude-sonnet-5-5"
        await pilot.press("enter")
        await pilot.pause()
        assert app.project.cfg.llm.model == "claude-sonnet-5-5"
        assert "claude-sonnet-5-5" in str(app.query_one("#header").render())

        from textual.widgets import TabbedContent

        from q3tui.tui.screens import SettingsScreen

        app.query_one("#cmd").value = "/model"                # no id: the settings' LLM tab, not the picker
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, SettingsScreen) and app.screen.query_one(TabbedContent).active == "set-tab-llm"
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("slash")

        app.query_one("#cmd").value = "/effort low --save"
        await pilot.press("enter")
        await pilot.pause()
        assert app.project.cfg.llm.effort == "low"
        assert "effort: low" in (app.project.root / "q3tui.yaml").read_text()

        await pilot.press("escape", "m")
        await pilot.pause()
        assert isinstance(app.screen, ModelScreen)
        custom = app.screen.query_one("#model-custom")
        custom.focus()
        custom.value = "claude-haiku-4-5"
        await pilot.press("enter")
        await pilot.pause()
        assert (app.project.cfg.llm.model, app.project.cfg.llm.effort) == ("claude-haiku-4-5", None)

        await pilot.press("r")  # next stage uses the new model
        await settle(app, pilot)
        assert llm.calls and app.project.cfg.llm.model == "claude-haiku-4-5"


async def test_spec_sections_panel_edits_template(app, llm):
    from textual.widgets import DataTable

    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.press("r")
        await settle(app, pilot)
        await pilot.press("l")  # leave the spec gate for later
        await pilot.pause()
        await pilot.press("1")
        await pilot.pause()
        assert app.selected_step == "spec"
        table = app.query_one("#spec-sections-table", DataTable)
        ids = [table.coordinate_to_cell_key((i, 0)).row_key.value for i in range(table.row_count)]
        assert ids[:2] == ["overview", "features"]

        table.focus()
        table.move_cursor(row=ids.index("verification"))
        await pilot.press("d")  # delete the optional Verification Notes section
        await pilot.pause()
        project_template = app.project.spec_dir / "template.yaml"
        assert project_template.is_file() and "verification" not in project_template.read_text()
        assert {v.name: v.status for v in app.engine.status()}["spec"] == "stale"

        table.move_cursor(row=0)
        await pilot.press("t")  # overview -> optional
        await pilot.pause()
        assert "required: false" in project_template.read_text().split("id: features")[0]

        await pilot.press("n")
        await pilot.pause()
        app.screen.query_one("Input").value = "Power Management"
        await pilot.press("enter")
        await pilot.pause()
        app.screen.query_one("TextArea").text = "Clock gating and power states"
        await pilot.press("ctrl+s")
        await pilot.pause()
        text = project_template.read_text()
        assert "id: power_management" in text and "Clock gating" in text


async def test_files_view_clears_after_reset(app, llm):

    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.press("r")
        await settle(app, pilot)
        await pilot.press("l")
        await pilot.pause()
        panel = app.query_one("#view-spec")
        viewer = panel.query_one("FileViewer")
        panel.show_tab("files")
        viewer.show(app.project.spec_dir / "spec.md")
        await pilot.pause()
        assert viewer.current and viewer.current.name == "spec.md"

        app.engine.reset("spec")
        app.refresh_all()
        await pilot.pause()
        # only intent.md (an input) is left; the deleted spec.md must not be shown
        assert viewer.current is None or viewer.current.name == "intent.md"
        assert "Elastic buffer" not in viewer.query_one("Markdown").source


async def test_chat_renders_markdown_and_does_not_drop_replies(app, monkeypatch):
    import anyio
    from rich.console import Group

    from q3tui.llm import runtime
    from q3tui.llm.runtime import StageResult

    release = anyio.Event()

    async def slow(stage, cfg, emit):
        emit("llm_text", stage=stage.name, text="**bold** and a list:\n\n- one\n- two")
        await release.wait()
        return StageResult("done", None, 0.0, 1, "s")

    monkeypatch.setattr(runtime, "run_stage", slow)
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.press("i")
        app.query_one("#cmd").value = "first question"
        await pilot.press("enter")
        await pilot.pause()
        app.query_one("#cmd").value = "second question"
        await pilot.press("enter")
        await pilot.pause()
        assert app.query_one("#cmd").value == "second question"  # kept, not sent
        assert app._assistant_worker.is_running  # first reply still in progress
        rendered = "\n".join(line.text for line in app.query_one("#activity").lines)
        assert "bold and a list" in rendered and "**bold**" not in rendered  # Markdown rendered
        release.set()
        await app.workers.wait_for_complete()

        await pilot.press("escape", "z")
        assert not app.query_one("#views").display
        await pilot.press("z")
        assert app.query_one("#views").display


async def test_selection_does_not_ping_pong(app, llm):
    """Programmatic selection (following the running step) must not bounce between views."""
    async with app.run_test(size=(140, 45)) as pilot:
        for name in ("spec", "rtl", "spec", "rtl"):
            app._select_step(name)          # quick succession, like step_finished + step_started
        for _ in range(5):
            await pilot.pause()
        assert app.selected_step == "rtl"
        seen = []
        orig = app._select_step
        app._select_step = lambda n: (seen.append(n), orig(n))[1]
        for _ in range(5):
            await pilot.pause()
        assert seen == []                   # no late events re-selecting anything
        await pilot.press("escape", "up")   # the user's own navigation still works
        await pilot.pause()
        assert app.selected_step == "spec"


async def test_command_history(app, llm):
    from q3tui.tui.screens import HistoryInput

    async with app.run_test(size=(140, 45)) as pilot:
        cmd = app.query_one("#cmd", HistoryInput)
        await pilot.press("i")
        for text in ("/status", "/cost"):
            cmd.value = text
            await pilot.press("enter")
            await pilot.pause()
        cmd.value = "half-typed"
        await pilot.press("up")
        assert cmd.value == "/cost"
        await pilot.press("up", "up")        # stops at the oldest
        assert cmd.value == "/status"
        await pilot.press("down", "down")    # back to the draft
        assert cmd.value == "half-typed"
    # persisted for the next session
    assert HistoryInput(history_file=app.project.state_dir / "tui" / "history.json").history[-2:] == ["/status", "/cost"]


async def test_reset_restarts_assistant_conversation(app, llm):
    from q3tui.tui.screens import ConfirmScreen

    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.press("r")
        await settle(app, pilot)
        await pilot.press("l")
        await pilot.pause()
        app.assistant.session_id = "old-session"
        app.action_reset_step("spec")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("y")
        await pilot.pause()
        assert app.assistant.session_id is None


async def test_approve_all_key(app, llm):
    from q3tui.tui.screens import ConfirmScreen

    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.press("r")
        await settle(app, pilot)
        await pilot.press("l")  # review later
        await pilot.pause()
        await pilot.press("A")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("y")
        await settle(app, pilot)
        assert app.engine.state.gates["spec"].status == "approved"
        assert app.engine.state.steps["rtl"].status == "done"   # the run continued by itself after approving


async def test_command_completion(app, llm):
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.press("slash")
        for ch in "appr":
            await pilot.press(ch)
        await pilot.pause()
        hint = app.query_one("#cmd-hint")
        assert hint.display and "/approve" in str(hint.render())
        await pilot.press("tab")
        await pilot.pause()
        assert app.query_one("#cmd").value == "/approve"



async def test_no_review_popup_at_startup(app, llm):
    from q3tui.core.project import Project
    from q3tui.tui.app import Q3TUIApp

    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.press("r")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert not isinstance(app.screen, GateScreen)  # reached during a run: no dialog over what you read
        await pilot.press("a")
        await pilot.pause()
        assert isinstance(app.screen, GateScreen)
    again = Q3TUIApp(Project.open(app.project.root))  # spec is still waiting for review
    async with again.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        await pilot.pause()
        assert not isinstance(again.screen, GateScreen)
        await pilot.press("a")  # still one key away
        await pilot.pause()
        await open_gate(again, pilot)
        assert isinstance(again.screen, GateScreen) and again.screen.step == "spec"


async def test_settings_dialog(app, llm):
    from textual.widgets import Checkbox, Input, Switch, TabbedContent

    from q3tui.tui.screens import SettingsScreen

    async with app.run_test(size=(160, 50)) as pilot:
        await pilot.press("comma")
        await pilot.pause()
        assert isinstance(app.screen, SettingsScreen)
        screen = app.screen
        assert screen.query_one(TabbedContent).active == "set-tab-spec"   # opens on the selected step's tab
        screen.query_one("#set-spec-self_review", Switch).value = False
        screen.query_one("#set-tb-max_turns", Input).value = "33"
        # saved to the project by default (no box to tick)
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert not isinstance(app.screen, SettingsScreen)
        assert app.project.cfg.spec.self_review is False and app.project.cfg.tb.max_turns == 33
        assert "max_turns: 33" in (app.project.root / "q3tui.yaml").read_text()

        await pilot.press("comma")
        await pilot.pause()
        app.screen.query_one("#set-rtl-parallel", Input).value = "abc"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, SettingsScreen)                       # stays open with the error shown
        assert "Modules in parallel" in str(app.screen.query_one("#settings-error").render()) or \
            "invalid" in str(app.screen.query_one("#settings-error").render())
        app.screen.query_one("#set-rtl-parallel", Input).value = "5"
        app.screen.query_one("#settings-session-only", Checkbox).value = True     # try it without saving
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.project.cfg.rtl.parallel == 5 and "parallel" not in (app.project.root / "q3tui.yaml").read_text()


async def test_stats_dialog(app, llm):
    from q3tui.tui.screens import StatsScreen

    app.bus.emit("llm_done", "spec", stage="spec_write", model="claude-haiku-4-5", cost_usd=0.25, duration_ms=5000, turns=1,
                 usage={"claude-haiku-4-5": {"input": 1000, "cache_read": 0, "cache_write": 0, "output": 500, "thinking": 100, "cost": 0.25}})
    app.session_start = "2999-01-01T00:00:00"     # the usage above belongs to an earlier session
    async with app.run_test(size=(160, 50)) as pilot:
        await pilot.press("s")
        await pilot.pause()
        assert isinstance(app.screen, StatsScreen)
        assert round(app.screen.summary.total.cost, 2) == 0.25 and app.screen.summary.total.tokens["thinking"] == 100
        assert app.screen.query_one("#stats-model").row_count == 1 and app.screen.query_one("#stats-step").row_count == 1
        await pilot.press("t")                                # this session only
        await pilot.pause()
        assert app.screen.query_one("#stats-model").row_count == 0
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, StatsScreen)


async def test_icon_sets(app, llm):
    from q3tui.core.icons import SETS, icon

    assert set(SETS["nerd"]) == set(SETS["unicode"]) == set(SETS["ascii"])      # every set has every icon
    async with app.run_test(size=(140, 45)) as pilot:
        assert "✔" in str(app.query_one("#legend").render())
        app.ops.apply_settings({"tui.icons": "nerd"})
        app.refresh_all()
        await pilot.pause()
        assert icon("done") == "" and "" in str(app.query_one("#legend").render())
        app.ops.apply_settings({"tui.icons": "ascii"})
        app.refresh_all()
        await pilot.pause()
        assert icon("review") == "#" and "✔" not in str(app.query_one("#legend").render())


async def test_escape_stops_the_assistant(app, llm, monkeypatch):
    import anyio

    started = anyio.Event()

    async def slow_ask(text, emit, context=None):
        started.set()
        await anyio.sleep(30)
        return "never"

    monkeypatch.setattr(app.assistant, "ask", slow_ask)
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.press("i")
        app.query_one("#cmd").value = "explain the arch"
        await pilot.press("enter")
        await started.wait()
        assert app._assistant_worker.is_running
        await pilot.press("escape")
        await pilot.pause()
        assert not app._assistant_worker.is_running
        lines = "\n".join(str(line.text) for line in app.query_one("#activity").lines)
        assert "assistant stopped" in lines
        await pilot.press("escape")                          # nothing to stop: leaves the input as before
        await pilot.pause()
        assert app.focused is app.query_one("#steps")


async def test_resizable_panes_are_remembered(app, llm):
    from q3tui.core.project import Project, read_json
    from q3tui.tui.app import Q3TUIApp

    async with app.run_test(size=(140, 45)) as pilot:
        left = app.query_one("#left")
        width = left.outer_size.width
        for _ in range(2):
            await pilot.press("ctrl+right")
            await pilot.pause()
        assert left.outer_size.width == width + 8
        await pilot.press("ctrl+down")
        await pilot.pause()
        await pilot.pause()
        saved = read_json(app.layout_file)
        assert saved["split-left"] == width + 8 and "split-activity" in saved
        # drag the spec sections splitter: the table column gets a fixed width
        splitter = app.query_one("#split-sections")
        splitter.set_size(30)
        app.remember_layout("split-sections", 30)
        # double-click resets a pane to its default
        app.query_one("#split-left").set_size(None)
        app.remember_layout("split-left", None)
        assert "split-left" not in read_json(app.layout_file)

    again = Q3TUIApp(Project.open(app.project.root))
    async with again.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        await pilot.pause()
        assert again.query_one("#views").outer_size.height == saved["split-activity"]  # the splitter's pane: step view (on top of activity)
        assert again.query_one("#split-sections").pane.styles.width.value == 30     # the sections column


async def test_reset_dialog_step_or_all(app, llm):
    from q3tui.tui.screens import ResetScreen

    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.press("r")
        await settle(app, pilot)
        await pilot.press("l")                              # spec review: later
        await pilot.pause()
        spec = app.project.spec_dir / "spec.md"
        assert spec.is_file()
        app.action_focus_steps()
        await pilot.press("R")
        await pilot.pause()
        assert isinstance(app.screen, ResetScreen)
        await pilot.press("escape")                          # cancel: nothing happens
        await pilot.pause()
        assert spec.is_file()
        await pilot.press("R")
        await pilot.pause()
        await pilot.press("a")                               # all steps
        await pilot.pause()
        assert not spec.is_file() and (app.project.spec_dir / "intent.md").is_file()   # your intent is kept
        assert app.engine.state.steps.get("spec") is None or app.engine.state.steps["spec"].status != "done"



async def test_answer_dialog_is_multiline_and_shows_the_whole_question(app, llm):
    from textual.widgets import TextArea

    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.press("r")
        await settle(app, pilot)
        await pilot.press("l")
        await pilot.pause()
        qid = next(q["id"] for q in app.engine.questions() if q["step"] == "spec")
        app.answer_question(qid)
        await pilot.pause()
        assert isinstance(app.screen, ChoiceScreen) and "Assumed so far" in app.screen.body     # the picker shows the whole question
        await pilot.press(str(len(app.screen.choices) + 1))                                      # Other… → type a longer answer
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, TextPromptScreen) and "Assumed so far" in str(screen.query_one("#prompt-body Static").render())
        area = screen.query_one("#prompt-area", TextArea)
        area.text = "line one"
        area.move_cursor(area.document.end)
        await pilot.press("enter")                          # a new line, not a submit
        await pilot.pause()
        assert isinstance(app.screen, TextPromptScreen)
        area.insert("line two")
        await pilot.press("ctrl+s")
        await pilot.pause()
        q = next(q for q in app.engine.questions() if q["id"] == qid)
        assert q["status"] == "answered" and q["answer"] == "line one\nline two"


async def test_every_step_view_opens_before_it_has_run(app, llm):
    async with app.run_test(size=(140, 45)) as pilot:
        for step in app.engine.steps:                       # e.g. clicking "tb" in the pipeline list
            app._select_step(step.name)
            await pilot.pause()
            assert app.selected_step == step.name


async def test_watching_another_run_shows_its_progress_not_run_hints(tmp_path):
    """A TUI opened on a folder where another process runs: its events in the activity log, no "next: run spec (r)"."""
    import json
    import os

    (tmp_path / "q3tui.yaml").write_text("{}\n")
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec" / "intent.md").write_text("A two-stage FIFO\n")
    project = Project.open(tmp_path)
    project.ensure_state_dir()
    (project.state_dir / "run.lock").write_text(json.dumps({"pid": os.getppid(), "started": "now"}))   # alive, not us
    run = project.state_dir / "runs" / "run_1"
    run.mkdir(parents=True)
    events = run / "events.jsonl"
    events.write_text(json.dumps({"ts": "2026-09-28T21:00:00.000", "kind": "step_started", "step": "spec",
                                  "title": "Specification", "feedback": []}) + "\n")
    app = Q3TUIApp(project, read_only=True)
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        text = "\n".join(str(line.text) for line in app.query_one("#activity").lines)
        assert "▶ Specification" in text and "another process" in text and "next: run spec" not in text
        with events.open("a") as f:                                                  # the other run goes on
            f.write(json.dumps({"ts": "2026-09-28T21:01:00.000", "kind": "step_started", "step": "req",
                                "title": "Requirements", "feedback": []}) + "\n")
        app._follow_run()
        await pilot.pause()
        text = "\n".join(str(line.text) for line in app.query_one("#activity").lines)
        assert "▶ Requirements" in text and text.count("▶ Specification") == 1


async def test_flow_editor_saves_step_settings(app, tmp_path):
    from textual.widgets import TextArea

    from q3tui.tui.screens import FlowScreen

    async with app.run_test(size=(140, 45)) as pilot:
        app.action_flow_editor()
        await pilot.pause()
        assert isinstance(app.screen, FlowScreen)
        app.screen.query_one("#flow-pass", TextArea).load_text("exists spec/spec.md")
        app.screen.query_one("#flow-notes", TextArea).load_text("be brief")
        await pilot.press("ctrl+s")
        await pilot.pause()
        spec = app.engine.flow.spec(app.selected_step)
        assert spec.passes == ["exists spec/spec.md"] and spec.notes == "be brief"
        assert "step_flow" in (tmp_path / "q3tui.yaml").read_text()


async def test_flow_editor_adds_and_moves_steps_and_prompts_editor(app, tmp_path):
    from textual.widgets import Input, TextArea

    from q3tui.tui.screens import FlowScreen, PromptsScreen

    async with app.run_test(size=(140, 45)) as pilot:
        app.action_flow_editor()
        await pilot.pause()
        scr = app.screen
        assert isinstance(scr, FlowScreen)
        n = len(scr.rows)
        scr.query_one("#flow-new-id", Input).value = "extra"
        scr._add()
        scr._move(-1)
        assert [r["step"] for r in scr.rows].count("extra") == 1 and len(scr.rows) == n + 1
        await pilot.press("ctrl+s")
        await pilot.pause()
        saved = [p for p in (tmp_path / "flows").iterdir()]
        assert saved and "extra" in saved[0].read_text()

        await pilot.press("P")
        await pilot.pause()
        assert isinstance(app.screen, PromptsScreen)
        app.screen.query_one("#pr-text", TextArea).load_text("always be brief")
        app.screen.action_save()
        assert (tmp_path / "prompts" / f"{app.screen.current}.md").read_text().endswith("always be brief\n")


def test_prompt_override_is_applied_to_stages(tmp_path):
    from q3tui.llm import prompts
    from q3tui.llm.runtime import Stage

    prompts.write(tmp_path, "rtl", "be brief")
    prompts.write(tmp_path, "rtl_fifo", "all of it", "replace")
    st = Stage(name="rtl_fifo", system_prompt="built-in", prompt="p", cwd=tmp_path)
    prompts.apply(st, tmp_path, "rtl", tmp_path / ".q3tui")
    assert st.system_prompt == "all of it"  # the stage's own file replaces what the step's file appended to
    assert "built-in" in prompts.builtin(tmp_path / ".q3tui", "rtl")
    assert prompts.read(tmp_path, "rtl") == ("append", "be brief")
    prompts.write(tmp_path, "rtl", "")
    assert prompts.find(tmp_path, "rtl") is None


async def test_skill_editor_forks_the_builtin_flow_and_saves(tmp_path, monkeypatch):
    from textual.widgets import TextArea

    from q3tui.core.project import Project
    from q3tui.tui.app import Q3TUIApp
    from q3tui.tui.screens import SkillScreen

    (tmp_path / "q3tui.yaml").write_text("llm: {model: claude-haiku-4-5}\npipeline: {flow: vlsit}\n")
    app = Q3TUIApp(Project.open(tmp_path))
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.press("K")
        await pilot.pause()
        assert isinstance(app.screen, SkillScreen)
        area = app.screen.query_one("#sk-text", TextArea)
        area.load_text(area.text.rstrip("\n") + "\n\nAlways answer briefly.\n")
        await pilot.press("ctrl+s")
        await pilot.pause()
        step = app.screen.current
        assert "Always answer briefly." in (tmp_path / "flows" / "vlsit" / step / "md" / "skill.md").read_text()
        assert app.engine.flow.spec(step).notes.endswith("Always answer briefly.")


async def test_comment_on_selection_goes_to_the_assistant(app, tmp_path, monkeypatch):
    from textual.widgets import TextArea

    from q3tui.tui.screens import TextPromptScreen

    sent = []
    monkeypatch.setattr(app, "_send_to_assistant", sent.append)
    monkeypatch.setattr(app, "selected_text", lambda: "reset value is 0")
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.press("C")
        await pilot.pause()
        assert isinstance(app.screen, TextPromptScreen)  # a selection is enough: the comment is about it
        app.screen.query_one(TextArea).load_text("make it 0x1800")
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert sent and "> reset value is 0" in sent[0] and "make it 0x1800" in sent[0]


async def test_markdown_prose_can_be_selected(app, tmp_path):
    from q3tui.tui.views import Markdown, MdProse

    (tmp_path / "spec").mkdir(exist_ok=True)
    (tmp_path / "spec" / "spec.md").write_text("# Title\n\nThe reset value is 0.\n")
    async with app.run_test(size=(140, 45)) as pilot:
        md = app.query(Markdown).first()
        md.update_md("# Title\n\nThe reset value is 0.\n")
        await pilot.pause()
        prose = md.query(MdProse).first()
        await pilot.pause()
        from textual.geometry import Offset
        from textual.selection import Selection

        got = prose.get_selection(Selection(Offset(0, 0), Offset(40, 3)))
        assert got and "reset value is 0" in got[0]


async def test_comment_on_the_highlighted_spec_section(app, tmp_path, monkeypatch):
    from textual.widgets import TextArea

    from q3tui.tui.screens import TextPromptScreen
    from q3tui.tui.views import SectionsPanel

    sent = []
    monkeypatch.setattr(app, "_send_to_assistant", sent.append)
    monkeypatch.setattr(app, "selected_text", lambda: "")
    (tmp_path / "spec").mkdir(exist_ok=True)
    (tmp_path / "spec" / "spec.md").write_text("# Spec\n\n## Overview\n\nA tiny FIFO.\n")
    async with app.run_test(size=(140, 45)) as pilot:
        app.selected_step = "spec"
        app._select_step("spec")
        app.refresh_all()
        await pilot.pause()
        panel = app.query_one(SectionsPanel)
        target = panel.comment_target()
        assert target is None or "section" in target[0]
        if target:
            await pilot.press("C")
            await pilot.pause()
            assert isinstance(app.screen, TextPromptScreen)
            app.screen.query_one(TextArea).load_text("expand it")
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert sent and "section" in sent[0] and "expand it" in sent[0]


async def test_comment_works_on_any_table_row(app, tmp_path, monkeypatch):
    from textual.widgets import DataTable, TextArea

    from q3tui.tui.screens import TextPromptScreen

    sent = []
    monkeypatch.setattr(app, "_send_to_assistant", sent.append)
    monkeypatch.setattr(app, "selected_text", lambda: "")
    async with app.run_test(size=(140, 45)) as pilot:
        app._select_step("rtl")
        await pilot.pause()
        app.current_panel().show_tab("questions")
        await pilot.pause()
        table = app.query_one("#view-rtl QuestionsTable DataTable", DataTable)
        table.add_row("Q-X1", "open", "Overflow?", "", key="Q-X1")  # a row, whatever filled it
        table.move_cursor(row=0)
        item = app.highlighted_item(app.current_panel())
        assert item and "Q-X1" in item[0] and "Overflow?" in item[1]
        await pilot.press("C")
        await pilot.pause()
        assert isinstance(app.screen, TextPromptScreen)
        app.screen.query_one(TextArea).load_text("it must saturate")
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert sent and "Q-X1" in sent[0] and "it must saturate" in sent[0]


def test_a_tree_without_a_code_fence_keeps_its_lines():
    from q3tui.tui.views import fence_diagrams, split_fences

    tree = "top\n├── a — x\n│   └── b\n└── c"
    assert split_fences("Hierarchy:\n\n" + tree + "\n\nAfter.") == [
        ("md", "Hierarchy:\n", ""), ("code", tree, "text"), ("md", "\nAfter.", "")]
    fenced = "```text\n" + tree + "\n```"
    assert fence_diagrams(fenced) == fenced  # already fenced: untouched
    assert fence_diagrams("one line ├── x") == "one line ├── x"  # a single line is just text
