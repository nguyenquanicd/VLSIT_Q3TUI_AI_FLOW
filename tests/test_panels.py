"""Flow-driven panels: a custom flow needs no TUI code; the spec flow's panels read its JSON artifacts."""

import json

import pytest

from q3tui.llm import runtime
from q3tui.pipeline.base import StepContext, StepDef
from q3tui.core.project import Project
from q3tui.steps import KINDS
from q3tui.tui.app import Q3TUIApp
from q3tui.tui.panels import DefaultPanel, PANELS, register_panel
from q3tui.tui.screens import GateScreen
from q3tui.tui.vlsit_panels import DataTab, VlsitPanel
from tests.fakes import FakeLLM


class NoteStep(StepDef):
    """A step kind the TUI has never heard of: writes notes/<id>.md from its dependencies' notes."""

    title = "Note"

    def inputs(self, engine):
        return [engine.project.root / "notes" / f"{d}.md" for d in self.deps]

    def outputs(self, engine):
        p = engine.project.root / "notes" / f"{self.name}.md"
        return [p] if p.is_file() else []

    async def run(self, ctx: StepContext) -> None:
        p = ctx.project.root / "notes" / f"{self.name}.md"
        p.parent.mkdir(exist_ok=True)
        p.write_text(f"# {self.name}\n\nbuilt from {', '.join(self.deps) or 'nothing'}\n")
        ctx.emit("log", message=f"{self.name} written")


@pytest.fixture
def llm(monkeypatch):
    fake = FakeLLM()
    monkeypatch.setattr(runtime, "run_stage", fake)
    return fake


@pytest.fixture
def mini(tmp_path, monkeypatch):
    monkeypatch.setitem(KINDS, "note", "tests.test_panels:NoteStep")
    (tmp_path / "flows").mkdir()
    (tmp_path / "flows" / "mini.yaml").write_text(
        "name: mini\ntitle: three notes\nsteps:\n"
        "  - {id: draft, kind: note, gate: human}\n"
        "  - {id: check, kind: note, deps: [draft], gate: auto}\n"
        "  - {id: final, kind: note, deps: [check]}\n")
    (tmp_path / "q3tui.yaml").write_text("pipeline: {flow: mini}\n")
    return Q3TUIApp(Project.open(tmp_path))


async def settle(app, pilot):
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


async def test_custom_flow_runs_in_the_tui_without_tui_code(mini, llm):
    app = mini
    async with app.run_test(size=(140, 45)) as pilot:
        assert [s.name for s in app.engine.pipeline_steps] == ["draft", "check", "final"]
        assert all(isinstance(app._panel(n), DefaultPanel) for n in ("draft", "check", "final"))
        assert "flow mini" in str(app.query_one("#header").render())
        assert [app.engine.label(n) for n in ("draft", "check", "final")] == ["1", "2", "3"]
        await pilot.press("2")                                              # number keys select by position
        await pilot.pause()
        assert app.selected_step == "check"
        assert "STEP 2 · NOTE" in str(app.query_one("#view-title").render())
        await pilot.press("1")
        await pilot.press("r")
        await settle(app, pilot)
        await pilot.press("a")                                              # (the review dialog opens on `a`)
        await pilot.pause()
        assert isinstance(app.screen, GateScreen) and app.screen.step == "draft"   # human gate on the first step
        await pilot.press("a")
        await settle(app, pilot)
        assert app.engine.state.gates["check"].status == "approved"         # auto gate signed itself
        assert all(v.status == "done" for v in app.engine.status())
        assert "auto" in str(app.query_one("#step-check").render())
        panel = app._panel("final")
        app._select_step("final")
        await pilot.pause()
        assert "notes/final.md" in [label for label, _ in panel.query_one("FileViewer")._options]   # its file on the Files tab
        assert any("final written" in msg for _, _, msg in app.step_log["final"])   # its Log tab


async def test_registered_panel_builders_win(mini, llm):
    built = []

    def build(step):
        built.append(step.name)
        return DefaultPanel(step)

    register_panel("note", build)
    try:
        app = mini
        async with app.run_test(size=(120, 40)):
            assert built == ["draft", "check", "final"]
    finally:
        PANELS.pop("note", None)


def _vlsit_project(tmp_path):
    (tmp_path / "q3tui.yaml").write_text("pipeline: {flow: spec}\n")
    s = tmp_path / "schemas"
    s.mkdir()
    (s / "structured_spec.json").write_text(json.dumps({
        "metadata": {"ip_name": "axi_ds", "gate_status": "pending"},
        "requirements": [
            {"req_id": "REQ-001", "text": "Split wide beats", "category": "functional", "ambiguity_score": 0.05, "needs_review": False},
            {"req_id": "REQ-002", "text": "TLAST on the last slice", "category": "interface", "ambiguity_score": 0.45, "needs_review": True,
             "rtl_modules": ["width_split"], "sva_hint": "property"}],
        "parameters": [{"name": "PR_IN_W", "type": "int", "default": "64", "valid_range": "32..128", "locked": False}]}))
    (s / "final_config.json").write_text(json.dumps({
        "metadata": {"gate_2_approved": True},
        "parameters": {"PR_IN_W": {"value": "64", "default": "64", "type": "int", "changed_from_default": False, "confirmed_by_user": True}},
        "constraints": {"C1": {"rule": "PR_IN_W % 32 == 0", "pass": True, "detail": "ok"}, "C2": {"rule": "x", "pass": False, "detail": "bad"}}}))


def _table_rows(app, panel_id, index=0):
    tables = list(app.query(f"#{panel_id} DataTab DataTable"))
    t = tables[index]
    return [[str(c) for c in t.get_row_at(i)] for i in range(t.row_count)]


async def test_spec_flow_panels_read_the_artifacts(tmp_path, llm):
    _vlsit_project(tmp_path)
    app = Q3TUIApp(Project.open(tmp_path))
    async with app.run_test(size=(180, 50)) as pilot:
        assert [s.name for s in app.engine.pipeline_steps] == ["spec", "parse", "config"]
        assert isinstance(app._panel("parse"), VlsitPanel) and isinstance(app._panel("config"), VlsitPanel)
        app.refresh_all()
        for name in ("parse", "config"):
            app._select_step(name)
            await pilot.pause()
        reqs = _table_rows(app, "view-parse")
        assert [r[0] for r in reqs] == ["REQ-001", "REQ-002"] and "0.45" in reqs[1][2] and "review" in reqs[1][3]
        assert _table_rows(app, "view-parse", 1)[0][0] == "PR_IN_W"
        cons = _table_rows(app, "view-config", 1)
        assert [r[0] for r in cons] == ["C1", "C2"] and cons[0][1] == "✔" and cons[1][1] == "✖"
        assert _table_rows(app, "view-config")[0][0] == "PR_IN_W"


def test_the_vlsit_panels_follow_the_icon_set():
    """✔ ✖ – were hardcoded: a font without them showed boxes whatever `tui.icons` said (unicode / nerd / ascii)."""
    from q3tui.core.icons import SETS, set_style
    from q3tui.tui import vlsit_panels as vp

    for style, (ok, bad, na) in {"unicode": ("✔", "✖", "–"), "ascii": ("+", "x", "-")}.items():
        set_style(style)
        assert (vp._tick(True).plain, vp._tick(False).plain, vp._tick(None).plain) == (ok, bad, na)
    set_style("nerd")
    assert vp._tick(True).plain == SETS["nerd"]["pass"] and vp._tick(None).plain == SETS["nerd"]["na"]
    assert all("na" in s for s in SETS.values())  # every set has one


def test_a_table_never_crashes_on_a_repeated_row_key():
    from textual.widgets import DataTable

    from q3tui.tui.views import fill_table

    t = DataTable()
    assert fill_table.__name__  # (the helper adds the rows; the key of a repeated one gets a suffix)
    keys: list[str] = []
    t.add_row = lambda *cells, key=None: keys.append(key)  # type: ignore[method-assign]
    t.clear = lambda: None  # type: ignore[method-assign]
    fill_table(t, [("a", "x"), ("a", "y"), ("b", "z")])
    assert keys == ["a", "a#2", "b"]
