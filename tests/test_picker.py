"""The picker (Claude-Code-style choice dialog), gate modes in the TUI, the answer / gate / reset / confirm dialogs on it."""

import pytest

from q3tui.llm import runtime
from q3tui.core.project import Project
from q3tui.tui.app import Q3TUIApp
from q3tui.tui.picker import Choice, ChoiceResult, ChoiceScreen, choices_from
from q3tui.tui.screens import ConfirmScreen, GateScreen, ResetScreen, SettingsScreen, TextPromptScreen
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


def _open(app, screen):
    got = []
    app.push_screen(screen, got.append)
    return got


def test_choices_from_questions_options():
    cs = choices_from([{"label": "16", "description": "small"}, "32", {"label": " "}], recommended="32")
    assert [(c.value, c.description, c.recommended) for c in cs] == [("16", "small", False), ("32", "", True)]
    assert ChoiceResult(["a", "b"]).answer == "a, b" and ChoiceResult([], "typed").answer == "typed"


async def test_single_select_keys(app):
    async with app.run_test(size=(120, 40)) as pilot:
        got = _open(app, ChoiceScreen("Pick", [Choice("a", "Alpha", "first"), Choice("b", "Beta", recommended=True), Choice("c", "Gamma")]))
        await pilot.pause()
        assert app.screen.query_one("OptionList").highlighted == 1        # the recommended one starts highlighted
        await pilot.press("down", "enter")
        await pilot.pause()
        assert got[0].value == "c"

        got = _open(app, ChoiceScreen("Pick", [Choice("a", "Alpha"), Choice("b", "Beta")]))
        await pilot.pause()
        await pilot.press("2")                                              # a number picks at once
        await pilot.pause()
        assert got[0].value == "b"

        got = _open(app, ChoiceScreen("Pick", [Choice("a", "Alpha")]))
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert got == [None]


async def test_other_types_an_answer(app):
    async with app.run_test(size=(120, 40)) as pilot:
        got = _open(app, ChoiceScreen("Pick", [Choice("a", "Alpha")], body="the question", other_value="old"))
        await pilot.pause()
        await pilot.press("2")                                              # Other…
        await pilot.pause()
        assert isinstance(app.screen, TextPromptScreen)
        app.screen.query_one("TextArea").text = "my own answer"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert got[0].text == "my own answer" and got[0].value is None and got[0].answer == "my own answer"


async def test_multi_select_and_preview(app):
    async with app.run_test(size=(120, 40)) as pilot:
        got = _open(app, ChoiceScreen("Pick some", [Choice("a", "Alpha", preview="about a"), Choice("b", "Beta", preview="about b"),
                                                     Choice("c", "Gamma")], multi=True))
        await pilot.pause()
        assert "about a" in str(app.screen.query_one("#choice-preview-text").render())
        await pilot.press("1", "3")                                         # toggles (multi-select)
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert got[0].values == ["a", "c"]


async def test_question_picker_options_and_default(app, llm):
    async with app.run_test(size=(140, 45)) as pilot:
        q = {"id": "Q-X1", "step": "spec", "question": "FIFO depth?", "blocking": True, "default_assumption": "16",
             "options": [{"label": "8", "description": "tiny"}, {"label": "32", "description": "big"}], "status": "open"}
        screen = app.question_picker(q)
        assert "blocking" in screen.title_text
        assert [c.value for c in screen.choices] == ["16", "8", "32"]       # the assumed answer first, recommended
        assert screen.choices[0].recommended
        got = _open(app, screen)
        await pilot.pause()
        await pilot.press("3")                                              # 32
        await pilot.pause()
        assert got[0].answer == "32"

        app.engine.by_name["rtl"].proposes_only = True                       # a step that only proposes changes
        proposal = app.question_picker({"id": "T1", "step": "rtl", "question": "Fix the spec?", "default_assumption": "change X",
                                        "status": "open"})
        assert [c.value for c in proposal.choices] == ["change X", "no"] and proposal.other == "Edit the proposal…"


async def test_enter_on_a_question_answers_through_the_picker(app, llm):
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.press("r")
        await settle(app, pilot)
        await pilot.press("l")
        await pilot.pause()
        qid = next(q["id"] for q in app.engine.questions() if q["step"] == "spec")
        assumed = next(q for q in app.engine.questions() if q["id"] == qid)["default_assumption"]
        app.answer_question(qid)
        await pilot.pause()
        assert isinstance(app.screen, ChoiceScreen)
        await pilot.press("enter" if assumed else "escape")                 # the assumed answer is highlighted: Enter records it
        await settle(app, pilot)
        if assumed:
            q = next(q for q in app.engine.questions() if q["id"] == qid)
            assert q["status"] == "answered" and q["answer"] == assumed


async def test_gate_screen_follows_the_state(app):
    async with app.run_test(size=(120, 40)) as pilot:
        got = _open(app, GateScreen("req", "summary", mode="auto", blocking=1, open_questions=2))
        await pilot.pause()
        values = [c.value for c in app.screen.choices]
        assert "approve" not in values and "questions" in values and "Gate mode: auto" in app.screen.body
        await pilot.press("q")
        await pilot.pause()
        assert got == ["questions"]
        got = _open(app, GateScreen("req", "summary"))
        await pilot.pause()
        assert [c.value for c in app.screen.choices][0] == "approve" and "questions" not in [c.value for c in app.screen.choices]
        await pilot.press("escape")
        await pilot.pause()
        assert got == ["later"]


async def test_confirm_and_reset_screens(app):
    async with app.run_test(size=(120, 40)) as pilot:
        got = _open(app, ConfirmScreen("Sure?", "detail"))
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert got == [True]
        got = _open(app, ConfirmScreen("Sure?"))
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert got == [False]
        got = _open(app, ResetScreen("req", {"req": []}, {"spec": [], "req": []}, str))
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        assert got == ["all"]


async def test_gate_mode_command_key_and_settings_tab(app, llm):
    async with app.run_test(size=(140, 45)) as pilot:
        assert app.engine.gate_mode("spec") == "human"
        await pilot.press("slash")
        app.query_one("#cmd").value = "/gate spec auto --session"
        await pilot.press("enter")
        await pilot.pause()
        assert app.engine.gate_mode("spec") == "auto"
        assert "auto" in str(app.query_one("#step-spec").render())          # the badge in the pipeline list
        assert "flow t" in str(app.query_one("#header").render())           # the flow name

        app.query_one("#cmd").value = "/gates"
        await pilot.press("enter")
        await pilot.pause()
        lines = [str(line.text) for line in app.query_one("#activity").lines]
        assert any("gates" in l for l in lines)

        await pilot.press("escape", "g")                                     # the picker for the selected step
        await pilot.pause()
        assert isinstance(app.screen, ChoiceScreen) and [c.value for c in app.screen.choices] == ["human", "auto", "auto_answer", "none"]
        await pilot.press("3")
        await pilot.pause()
        assert app.engine.gate_mode("spec") == "auto_answer"
        assert "auto_answer" in (app.project.root / "q3tui.yaml").read_text()   # kept for next time

        app.action_settings("gates")
        await pilot.pause()
        assert isinstance(app.screen, SettingsScreen)
        from textual.widgets import Select, TabbedContent

        assert app.screen.query_one(TabbedContent).active == "set-tab-gates"
        app.screen.query_one("#set-gate-spec", Select).value = "none"
        app.screen.action_apply()
        await pilot.pause()
        assert app.engine.gate_mode("spec") is None


async def test_auto_gate_approves_itself_in_a_run(app, llm):
    app.project.cfg.pipeline.gate_modes = {"spec": "auto", "rtl": "human"}
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.press("r")
        await settle(app, pilot)
        assert app.engine.state.gates["spec"].status == "approved"          # no dialog for spec; the run went on
        # rtl's human gate opened, but no dialog pops up over what you are reading (_maybe_show_gate): a note
        # says it is waiting (app._gate_shown) and `a` opens the review on demand
        assert not isinstance(app.screen, GateScreen)
        assert app._gate_shown == "rtl"
        await pilot.press("a")
        await pilot.pause()
        assert isinstance(app.screen, GateScreen) and app.screen.step == "rtl"
