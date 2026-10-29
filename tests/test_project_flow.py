"""The project settings flow: scanning a folder for libraries, adding / removing a technology in the project's pdks.json, tools and modules, the
steps' reports, and the three views."""

import json
import sys
from pathlib import Path

import pytest

from q3tui import flows
from q3tui.core.ops import Ops
from q3tui.core.project import Project
from q3tui.flows.asic.lib import pdks
from q3tui.flows.fpga.lib import hints
from q3tui.flows.project.lib import common
from q3tui.pipeline.engine import Engine, EngineError
from q3tui.tui.app import Q3TUIApp


@pytest.fixture
def proj(tmp_path):
    for tech, corners in (("cells65", ("ss_1p08v", "tt_1p2v", "ff_1p32v")), ("cells28", ("tt",))):
        (tmp_path / "pdk" / tech / "lib").mkdir(parents=True)
        for c in corners:
            (tmp_path / "pdk" / tech / "lib" / f"{tech}_{c}.db").write_text("x")
    (tmp_path / "pdk" / "cells65" / "lib" / "tech.db").write_text("not a cell library")
    (tmp_path / "q3tui.yaml").write_text("pipeline: {flow: project}\n")
    return Project.open(tmp_path)


# -- the flow ---------------------------------------------------------------------------------------------------------------------------


def test_flow_loads():
    flow = flows.load_flow("project", Path.cwd())
    assert [s.id for s in flow.steps] == ["project", "tools", "libraries", "fpga"] and flow.gates == {}
    assert {"lib_scan", "lib_add", "lib_remove", "lib_list", "lib_use", "tools_check", "tools_modules",
            "fpga_list", "fpga_use", "fpga_add", "fpga_remove"} <= set(flow.actions)


# -- scanning and adding a library ---------------------------------------------------------------------------------------------------------


def test_scan_groups_the_db_files_by_folder(proj):
    found = common.scan(str(proj.root / "pdk"))
    assert [(Path(g["dir"]).parent.name, len(g["files"])) for g in found["groups"]] == [("cells65", 4), ("cells28", 1)]  # the biggest folder first
    assert found["groups"][0]["glob"] == str(proj.root / "pdk" / "cells65" / "lib" / "*.db")
    assert len(common.scan(str(proj.root / "pdk" / "*" / "lib" / "*_tt*.db"))["groups"][0]["files"]) == 1  # a glob
    text = common.describe_scan(found)
    assert "4 .db" in text and "ss_1p08v" in text and "cells65" in text
    assert common.describe_scan({"groups": []}) == "no .db file found"
    for bad in ("", str(proj.root / "nope")):
        with pytest.raises(ValueError):
            common.scan(bad)


def test_adding_a_technology_writes_the_projects_pdks_json(proj):
    msg = common.add_entry(proj, "Cells 65", str(proj.root / "pdk" / "cells65" / "lib"), "Cells 65 nm LP", "65", default="tt_1p2v", probe_ns="0.5", include="^cells65_")
    assert "'cells_65'" in msg and "3 corner(s)" in msg  # (tech.db does not match `include`)
    data = json.loads((proj.root / "config" / "pdks.json").read_text())
    assert data["pdks"] == [{"id": "cells_65", "title": "Cells 65 nm LP", "node_nm": 65, "globs": [str(proj.root / "pdk" / "cells65" / "lib" / "*.db")],
                             "include": "^cells65_", "default": "tt_1p2v", "probe_ns": 0.5}]
    tech = next(p for p in pdks.catalog(proj) if p.id == "cells_65")  # the ASIC flow's dialogs offer it
    assert [c for c, _ in tech.corners] == ["ff_1p32v", "ss_1p08v", "tt_1p2v"] and tech.default_path().endswith("cells65_tt_1p2v.db") and tech.probe_ns == 0.5
    common.add_entry(proj, "cells_65", str(proj.root / "pdk" / "cells65" / "lib" / "*.db"), "Renamed")  # the same id replaces it
    assert [e["title"] for e in json.loads((proj.root / "config" / "pdks.json").read_text())["pdks"]] == ["Renamed"]


def test_what_cannot_be_added_says_why(proj):
    with pytest.raises(ValueError, match="several folders"):
        common.add_entry(proj, "x", str(proj.root / "pdk"))  # the .db files of two technologies: name one
    with pytest.raises(ValueError, match="not one of: ff_1p32v, ss_1p08v, tech, tt_1p2v"):  # (tech.db is not of the cell family: it keeps its name)
        common.add_entry(proj, "x", str(proj.root / "pdk" / "cells65" / "lib"), default="slow")
    with pytest.raises(ValueError, match="matches no .db"):
        common.add_entry(proj, "x", str(proj.root / "pdk" / "cells65" / "lib"), include="^nothing")
    with pytest.raises(ValueError, match="needs an id"):
        common.add_entry(proj, "  ", str(proj.root / "pdk" / "cells65" / "lib"))
    assert not (proj.root / "config" / "pdks.json").exists()  # nothing half written
    (proj.root / "config").mkdir(exist_ok=True)
    (proj.root / "config" / "pdks.json").write_text("{not json")
    with pytest.raises(ValueError, match="not valid JSON"):
        common.add_entry(proj, "x", str(proj.root / "pdk" / "cells65" / "lib"))


def test_removing_a_technology(proj):
    common.add_entry(proj, "mine", str(proj.root / "pdk" / "cells28" / "lib"))
    assert "removed" in common.remove_entry(proj, "mine") and json.loads((proj.root / "config" / "pdks.json").read_text())["pdks"] == []
    with pytest.raises(ValueError, match="built-in technology"):
        common.remove_entry(proj, "gf180")
    with pytest.raises(ValueError, match="no technology 'ghost'"):
        common.remove_entry(proj, "ghost")


def test_the_actions_are_the_assistants_and_the_clis(proj):
    ops = Ops(Engine(proj))
    assert "cells65" in ops.run_action("lib_scan", folder=str(proj.root / "pdk"))
    assert "'c28'" in ops.run_action("lib_add", id="c28", folder=str(proj.root / "pdk" / "cells28" / "lib"), node_nm="28")
    listing = ops.run_action("lib_list")
    assert "c28 | c28 | 28 nm | project | 1 corners: cells28_tt" in listing and "gf180 |" in listing and listing.rstrip().splitlines()[-1].startswith("in use:")
    assert "saved to q3tui.yaml" in ops.run_action("lib_use", id="c28")
    assert hints.asic_library(proj).endswith("cells28_tt.db") and "cells28_tt.db" in (proj.root / "q3tui.yaml").read_text()  # both flows read it
    with pytest.raises(EngineError, match="no technology 'nope'"):
        ops.run_action("lib_use", id="nope")
    with pytest.raises(EngineError, match="not a corner of c28"):
        ops.run_action("lib_use", id="c28", corner="zz")
    ops.run_action("lib_remove", id="c28")
    with pytest.raises(EngineError, match="no folder|not a folder|needs"):
        ops.run_action("lib_add", id="z")  # (the folder is required)


# -- tools ---------------------------------------------------------------------------------------------------------------------------------


def test_the_tool_roles_and_where_they_come_from(proj, monkeypatch):
    from q3tui.eda import tools as toolmod

    monkeypatch.setattr(toolmod, "available", lambda tools, role: role == "mylint")
    (proj.root / "q3tui.yaml").write_text(f"pipeline: {{flow: project}}\ntools:\n  modules: [some/module]\n  roles:\n    mylint: {{cmd: \"{sys.executable} {{file}}\", parse: generic, description: my linter}}\n")
    proj = Project.open(proj.root)
    rows = {r["role"]: r for r in common.tool_rows(proj)}
    assert rows["mylint"]["found"] and rows["mylint"]["from"] == "q3tui.yaml tools.roles" and rows["mylint"]["tool"] == sys.executable and "some/module" in rows["mylint"]["modules"]
    assert rows["fpga_synth"]["from"] == "not configured" and rows["fpga_synth"]["used_by"] == ["fpga"] and not rows["fpga_synth"]["found"]  # no flow brings a default
    assert rows["asic_lec"]["from"] == "not configured" and rows["fpga_pnr_ecp5"]["tool"] == ""  # (listed, so the TUI shows what is missing)
    assert [r["role"] for r in common.load_tool_rows(proj)] == [r["role"] for r in common.tool_rows(proj, check=False)]
    toolmod.write_preset(proj.config_dir)  # `tools init`: every role of every flow, now the project's own
    rows = {r["role"]: r for r in common.tool_rows(Project.open(proj.root))}
    assert rows["fpga_synth"]["from"].endswith("tools.json") and rows["asic_lec"]["modules"][-1] == "synopsys/formality/V-2023.12" and rows["fpga_pnr_ecp5"]["tool"] == "nextpnr-ecp5"


def test_the_modules_loaded_before_every_tool(proj):
    ops = Ops(Engine(proj))
    assert proj.cfg.tools.modules == []
    assert "synopsys/syn, oss-cad-suite" in ops.run_action("tools_modules", modules="synopsys/syn  oss-cad-suite")
    assert proj.cfg.tools.modules == ["synopsys/syn", "oss-cad-suite"] and Project.open(proj.root).cfg.tools.modules == ["synopsys/syn", "oss-cad-suite"]
    assert "none" in ops.run_action("tools_modules") and Project.open(proj.root).cfg.tools.modules == []


# -- the steps ---------------------------------------------------------------------------------------------------------------------------------


async def test_the_steps_run_and_report(proj, monkeypatch):
    from q3tui.eda import tools as toolmod

    monkeypatch.setattr(toolmod, "available", lambda tools, role: role.startswith("asic"))
    toolmod.write_preset(proj.config_dir)  # (the project's tools file: every flow's roles)
    common.add_entry(proj, "c28", str(proj.root / "pdk" / "cells28" / "lib"))
    eng = Engine(Project.open(proj.root))
    await eng.run(yes=True)
    assert all(v.status == "done" for v in eng.status()), [(v.name, v.status, v.detail) for v in eng.status()]
    rep = lambda n: json.loads((proj.state_dir / "project" / f"{n}.json").read_text())  # noqa: E731
    assert rep("project")["flow"] == "project" and rep("project")["files"] == {"q3tui.yaml": True, "tools.json": True, "pdks.json": True} and rep("project")["problems"] == []
    t = rep("tools")
    assert "asic_synth" not in t["missing"] and "asic_lec" not in t["missing"] and "fpga_synth" in t["missing"]  # (the stand-in finds the asic roles only)
    assert common.load_tool_rows(proj) and t["found"] + len(t["missing"]) == t["roles"]
    lib = rep("libraries")
    assert {x["id"] for x in lib["technologies"]} >= {"gf180", "c28"} and lib["library"].endswith("tt_025C_5v00.db")
    # a change of the project's files makes the report stale
    common.remove_entry(proj, "c28")
    assert {v.name: v.status for v in Engine(Project.open(proj.root)).status()}["libraries"] == "stale"


async def test_a_technology_without_files_is_reported(proj, monkeypatch):
    from q3tui.eda import tools as toolmod

    monkeypatch.setattr(toolmod, "available", lambda tools, role: False)
    common.save_pdks(proj, {"pdks": [{"id": "ghost", "title": "Not here", "node_nm": 7, "globs": [str(proj.root / "nowhere" / "*.db")]}]})
    eng = Engine(Project.open(proj.root))
    await eng.run(yes=True)  # (the steps run in order: the checks before it first)
    assert any("technology 'ghost' (project) matches no .db" in p for p in json.loads((proj.state_dir / "project" / "libraries.json").read_text())["problems"])


# -- the views ------------------------------------------------------------------------------------------------------------------------------------


def _tab(app, name: str):
    (w,) = [w for w in app.query(name)]  # (the flow's panels.py is loaded by path: its classes are found by name)
    return w


async def test_the_project_view_edits_settings_in_place(proj):
    from textual.widgets import DataTable, Select

    from q3tui.tui.screens import EditScreen

    app = Q3TUIApp(proj)
    async with app.run_test(size=(150, 50)) as pilot:
        await pilot.pause()
        tab = _tab(app, "SettingsTab")
        tab.refresh_view()
        table = tab.query_one(DataTable)
        keys = [table.coordinate_to_cell_key((i, 0)).row_key.value for i in range(table.row_count)]
        assert keys[:3] == ["info.root", "info.config", "info.flow"] and "pipeline.auto_approve" in keys and "llm.model" in keys
        tab.action_edit()  # the first row is information
        await pilot.pause()
        assert not isinstance(app.screen, EditScreen)
        table.move_cursor(row=keys.index("pipeline.auto_approve"))
        await pilot.pause()
        tab.action_edit()
        await pilot.pause()
        assert isinstance(app.screen, EditScreen)
        sel = app.screen.query_one("#f-v", Select)
        assert sel.value in ("on", "off")
        sel.value = "on" if sel.value == "off" else "off"
        want = sel.value == "on"
        app.screen.action_save()
        await pilot.pause()
    assert proj.cfg.pipeline.auto_approve is want and ("auto_approve: " + str(want).lower()) in (proj.root / "q3tui.yaml").read_text()  # live and saved


class GuessLLM:
    """A stand-in for the identify stage: records the stage, answers with a canned LibGuess."""

    def __init__(self, guess=None, fail=None):
        self.guess, self.fail, self.stages = guess, fail, []

    async def __call__(self, stage, cfg, emit):
        from q3tui.llm.runtime import StageResult

        self.stages.append(stage)
        if self.fail:
            raise self.fail
        return StageResult("ok", self.guess, 0.02, 3, "s")


def _guess(root, **kw):
    g = dict(id="Cells 65!", title="Cells 65 nm LP", node_nm=65, foundry="Acme", glob=str(root / "pdk" / "cells65" / "lib" / "*.db"), include="^cells65_",
             default_corner="tt_1p2v", probe_ns=0.5, notes="Typical corner tt_1p2v; tech.db left out.")
    g.update(kw)
    return common.LibGuess(**g)


def test_the_llm_fills_in_the_rest_and_code_checks_it(proj, monkeypatch):
    from q3tui.llm import runtime

    fake = GuessLLM(_guess(proj.root))
    monkeypatch.setattr(runtime, "run_stage", fake)
    ops = Ops(Engine(proj))
    g = common.identify(ops, str(proj.root / "pdk" / "cells65"))
    (stage,) = fake.stages
    assert stage.name == "lib_identify" and set(stage.builtin_tools) == {"Read", "Grep", "Glob"} and stage.output_model is common.LibGuess  # read only
    assert stage.cwd == proj.root / "pdk" / "cells65" and "You identify a standard-cell library folder" in stage.system_prompt
    assert "cells65_ff_1p32v" in stage.prompt and "4 .db file(s)" in stage.prompt and "tech" in stage.prompt  # what code found: the ground truth
    assert (g["id"], g["title"], g["node_nm"], g["include"], g["default"], g["probe_ns"], g["warnings"]) == ("cells_65", "Cells 65 nm LP", "65", "^cells65_", "tt_1p2v", "0.5", [])
    assert g["corners"] == ["ff_1p32v", "ss_1p08v", "tt_1p2v"]  # (tech.db is excluded by the include)


def test_what_the_llm_got_wrong_is_dropped_and_said(proj, monkeypatch):
    from q3tui.llm import runtime

    bad = _guess(proj.root, glob="/nowhere/*.db", include="(unclosed", default_corner="turbo")
    monkeypatch.setattr(runtime, "run_stage", GuessLLM(bad))
    g = common.identify(Ops(Engine(proj)), str(proj.root / "pdk" / "cells65"))
    assert g["folder"].endswith("cells65/lib/*.db") and g["include"] == "" and g["default"] == ""
    assert [w.split(" ")[0] for w in g["warnings"]] == ["the", "include", "default"] and any("is not a valid regex" in w for w in g["warnings"])
    monkeypatch.setattr(runtime, "run_stage", GuessLLM(_guess(proj.root, include="^nothing")))
    assert "matches no file: dropped" in common.identify(Ops(Engine(proj)), str(proj.root / "pdk" / "cells65"))["warnings"][0]
    with pytest.raises(ValueError, match="no .db file"):
        common.identify(Ops(Engine(proj)), str(proj.root / "pdk" / "nothing-here")) if (proj.root / "pdk" / "nothing-here").mkdir() is None else None


def test_lib_add_with_only_the_folder_uses_the_llm_and_what_you_give_wins(proj, monkeypatch):
    from q3tui.llm import runtime

    fake = GuessLLM(_guess(proj.root))
    monkeypatch.setattr(runtime, "run_stage", fake)
    ops = Ops(Engine(proj))
    msg = ops.run_action("lib_add", folder=str(proj.root / "pdk" / "cells65"), title="My name")
    assert "'cells_65'" in msg and "Typical corner tt_1p2v" in msg and len(fake.stages) == 1
    e = json.loads((proj.root / "config" / "pdks.json").read_text())["pdks"][0]
    assert (e["id"], e["title"], e["node_nm"], e["default"], e["include"], e["probe_ns"]) == ("cells_65", "My name", 65, "tt_1p2v", "^cells65_", 0.5)  # the title was given: it wins
    ops.run_action("lib_add", id="c28", folder=str(proj.root / "pdk" / "cells28" / "lib"))  # an id: no LLM
    assert len(fake.stages) == 1
    assert "cells_65 | Cells 65 nm LP | 65 nm | Acme" in ops.run_action("lib_identify", folder=str(proj.root / "pdk" / "cells65")) and len(fake.stages) == 2
    assert [e["id"] for e in json.loads((proj.root / "config" / "pdks.json").read_text())["pdks"]] == ["cells_65", "c28"]  # (identify wrote nothing)


async def test_the_libraries_view_adds_with_only_a_path(proj, monkeypatch):
    from textual.widgets import Button, DataTable, Input, Select

    from q3tui.llm import runtime
    from q3tui.tui.screens import EditScreen

    fake = GuessLLM(_guess(proj.root))
    monkeypatch.setattr(runtime, "run_stage", fake)
    app = Q3TUIApp(proj)
    async with app.run_test(size=(160, 60)) as pilot:
        await pilot.pause()
        app._select_step("libraries")
        await pilot.pause()
        tab = _tab(app, "LibTab")
        tab.refresh_view()
        table = tab.query_one(DataTable)
        assert {"gf180", "freepdk45"} <= {table.coordinate_to_cell_key((i, 0)).row_key.value for i in range(table.row_count)}  # the built-in open-source ones
        tab.action_add()
        await pilot.pause()
        assert isinstance(app.screen, EditScreen) and [str(b.label) for b in app.screen.query(Button)] == ["Save (ctrl+s)", "Cancel (esc)"]  # one question: the path
        assert [f[0] for f in app.screen.fields] == ["folder"]
        app.screen.query_one("#f-folder", Input).value = str(proj.root / "pdk" / "cells65")
        app.screen.action_save()
        for _ in range(40):  # (the LLM stage runs in a thread, then the form opens)
            await pilot.pause(0.25)
            if isinstance(app.screen, EditScreen) and "id" in [f[0] for f in app.screen.fields]:
                break
        scr = app.screen
        assert isinstance(scr, EditScreen) and scr.title_text == "Add a technology" and len(fake.stages) == 1
        assert scr.query_one("#f-id", Input).value == "cells_65" and scr.query_one("#f-title", Input).value == "Cells 65 nm LP" and scr.query_one("#f-node_nm", Input).value == "65"
        default = scr.query_one("#f-default", Select)  # the corners of its files, the LLM's pick selected
        assert default.value == "tt_1p2v" and [v for _l, v in default._options if v != Select.BLANK] == ["(first)", "ff_1p32v", "ss_1p08v", "tt_1p2v"]
        assert "Typical corner tt_1p2v" in scr.hint
        scr.query_one("#f-use", Select).value = "yes"
        scr.action_save()
        await pilot.pause()
        tab.refresh_view()
        keys = [table.coordinate_to_cell_key((i, 0)).row_key.value for i in range(table.row_count)]
        assert "cells_65" in keys
    assert hints.asic_library(proj).endswith("cells65_tt_1p2v.db")  # used as the ASIC library: its default corner
    assert json.loads((proj.root / "config" / "pdks.json").read_text())["pdks"][0]["default"] == "tt_1p2v"


async def test_when_the_llm_cannot_the_form_opens_to_fill_in_by_hand(proj, monkeypatch):
    from textual.widgets import Input

    from q3tui.llm import runtime
    from q3tui.tui.screens import EditScreen

    monkeypatch.setattr(runtime, "run_stage", GuessLLM(fail=runtime.StageError("no credentials")))
    app = Q3TUIApp(proj)
    async with app.run_test(size=(160, 60)) as pilot:
        await pilot.pause()
        app._select_step("libraries")
        await pilot.pause()
        tab = _tab(app, "LibTab")
        tab.action_add()
        await pilot.pause()
        app.screen.query_one("#f-folder", Input).value = str(proj.root / "pdk" / "cells65" / "lib")
        app.screen.action_save()
        for _ in range(40):
            await pilot.pause(0.25)
            if isinstance(app.screen, EditScreen) and "id" in [f[0] for f in app.screen.fields]:
                break
        assert isinstance(app.screen, EditScreen) and "Not identified" in app.screen.hint
        assert app.screen.query_one("#f-folder", Input).value.endswith("cells65/lib") and app.screen.query_one("#f-id", Input).value == ""
        app.screen.dismiss(None)


async def test_enter_edits_a_technology_and_u_picks_the_corner(proj):
    from textual.widgets import DataTable, Select

    from q3tui.tui.screens import EditScreen

    common.add_entry(proj, "cells65", str(proj.root / "pdk" / "cells65" / "lib"), "Cells 65", "65", include="^cells65_", default="tt_1p2v")
    mine = Project.open(proj.root)  # (the app's own project object: its live config is what the view changes)
    app = Q3TUIApp(mine)
    async with app.run_test(size=(160, 60)) as pilot:
        await pilot.pause()
        app._select_step("libraries")
        await pilot.pause()
        tab = _tab(app, "LibTab")
        tab.refresh_view()
        table = tab.query_one(DataTable)
        keys = [table.coordinate_to_cell_key((i, 0)).row_key.value for i in range(table.row_count)]
        table.move_cursor(row=keys.index("cells65"))
        table.focus()
        await pilot.pause()
        await pilot.press("enter")  # as in the other flows: Enter edits the row
        await pilot.pause()
        scr = app.screen
        assert isinstance(scr, EditScreen) and scr.title_text == "Technology cells65" and "id" not in [f[0] for f in scr.fields]
        assert scr.query_one("#f-default", Select).value == "tt_1p2v" and scr.query_one("#f-use", Select).value == "no"
        scr.query_one("#f-default", Select).value = "ss_1p08v"
        scr.query_one("#f-use", Select).value = "yes"
        scr.action_save()
        await pilot.pause()
        assert hints.asic_library(mine).endswith("cells65_ss_1p08v.db")  # the default changed, and it is the library in use
        tab.refresh_view()
        tab.action_use()  # u: another corner of it
        await pilot.pause()
        scr = app.screen
        assert isinstance(scr, EditScreen) and scr.query_one("#f-corner", Select).value == "ss_1p08v"  # the one in use is preselected
        scr.query_one("#f-corner", Select).value = "ff_1p32v"
        scr.action_save()
        await pilot.pause()
    assert hints.asic_library(Project.open(proj.root)).endswith("cells65_ff_1p32v.db")  # saved to q3tui.yaml
    assert json.loads((proj.root / "config" / "pdks.json").read_text())["pdks"][0]["default"] == "ss_1p08v"  # (u does not change the technology's default)


async def test_enter_edits_a_setting_too(proj):
    from textual.widgets import DataTable

    from q3tui.tui.screens import EditScreen

    app = Q3TUIApp(proj)
    async with app.run_test(size=(150, 50)) as pilot:
        await pilot.pause()
        tab = _tab(app, "SettingsTab")
        tab.refresh_view()
        table = tab.query_one(DataTable)
        keys = [table.coordinate_to_cell_key((i, 0)).row_key.value for i in range(table.row_count)]
        table.move_cursor(row=keys.index("llm.effort"))
        table.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, EditScreen) and app.screen.title_text == "Effort"


# -- editing a tool role ----------------------------------------------------------------------------------------------------------------------


def test_a_role_is_saved_where_the_project_keeps_its_roles(proj):
    # no tools file and no inline roles: tools.json is created from the presets, so the other roles stay; the role edited starts from its template
    msg = common.set_role(proj, "lint", cmd="mylint -f {filelist}", modules="my/lint", timeout_s="120")
    assert msg == "tool role 'lint' saved to tools.json"
    data = json.loads((proj.root / "config" / "tools.json").read_text())
    assert data["roles"]["lint"]["cmd"] == "mylint -f {filelist}" and data["roles"]["lint"]["modules"] == ["my/lint"] and data["roles"]["lint"]["timeout_s"] == 120
    assert {"synth", "sim", "run", "verible_lint", "fpga_synth"} <= set(data["roles"]) and "_comment" not in data  # (the presets' others are kept)
    assert data["roles"]["lint"]["parse"] == "vcs"  # what the role had stays
    from q3tui.eda import tools as toolmod

    assert toolmod.load_tools(Project.open(proj.root).cfg, proj.root).roles["lint"].cmd == "mylint -f {filelist}"  # and is read at once
    common.set_role(proj, "synth", cmd="dc_shell -f {script} -x")  # another role: updates the same file
    assert json.loads((proj.root / "config" / "tools.json").read_text())["roles"]["lint"]["cmd"] == "mylint -f {filelist}"
    with pytest.raises(ValueError, match="not defined: give its command"):
        common.set_role(proj, "nonesuch")
    assert "saved to tools.json" in common.set_role(proj, "nonesuch", cmd="x {a}")


def test_a_role_the_file_lacks_is_edited_from_its_template(proj):
    common.set_role(proj, "fpga_synth", modules="xilinx/vivado/2024.1")  # the FPGA template (Vivado), another module
    r = json.loads((proj.root / "config" / "tools.json").read_text())["roles"]["fpga_synth"]
    assert r["modules"] == ["xilinx/vivado/2024.1"] and r["cmd"].startswith("vivado -mode batch") and r["parse"] == "vivado"


def test_roles_inline_in_q3tui_yaml_and_other_files(proj):
    (proj.root / "q3tui.yaml").write_text("pipeline: {flow: project}\ntools:\n  roles:\n    mylint: {cmd: \"mylint {file}\", parse: generic}\n")
    proj = Project.open(proj.root)
    assert "saved to q3tui.yaml" in common.set_role(proj, "mylint", cmd="mylint2 {file}")
    assert Project.open(proj.root).cfg.tools.roles["mylint"].cmd == "mylint2 {file}" and not (proj.root / "config" / "tools.json").exists()
    (proj.root / "config").mkdir(exist_ok=True)
    (proj.root / "config" / "tools.yaml").write_text("roles: {}\n")
    with pytest.raises(ValueError, match="tools.yaml: edit it by hand"):
        common.set_role(Project.open(proj.root), "mylint", cmd="x")


async def test_enter_edits_a_tool_role(proj, monkeypatch):
    from textual.widgets import DataTable, Input

    from q3tui.eda import tools as toolmod
    from q3tui.tui.screens import EditScreen

    monkeypatch.setattr(toolmod, "available", lambda tools, role: True)
    toolmod.write_preset(proj.config_dir)
    app = Q3TUIApp(proj)
    async with app.run_test(size=(150, 50)) as pilot:
        await pilot.pause()
        app._select_step("tools")
        await pilot.pause()
        tab = _tab(app, "ToolsTab")
        tab.action_check()
        for _ in range(60):
            await pilot.pause(0.25)
            if (proj.state_dir / "project" / "tool_rows.json").is_file():
                break
        await pilot.pause(0.5)
        tab.refresh_view()
        table = tab.query_one(DataTable)
        keys = [table.coordinate_to_cell_key((i, 0)).row_key.value for i in range(table.row_count)]
        table.move_cursor(row=keys.index("asic_lec"))
        table.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, EditScreen) and app.screen.title_text == "Tool role asic_lec"
        assert app.screen.query_one("#f-cmd", Input).value == "fm_shell -f {script}"
        app.screen.query_one("#f-cmd", Input).value = "fm_shell -f {script} -no_gui"
        app.screen.action_save()
        await pilot.pause()
    assert json.loads((proj.root / "config" / "tools.json").read_text())["roles"]["asic_lec"]["cmd"] == "fm_shell -f {script} -no_gui"


async def test_the_tools_view_checks_and_sets_modules(proj, monkeypatch):
    from textual.widgets import DataTable, Input

    from q3tui.eda import tools as toolmod
    from q3tui.tui.screens import EditScreen

    monkeypatch.setattr(toolmod, "available", lambda tools, role: role == "fpga_synth")
    app = Q3TUIApp(proj)
    async with app.run_test(size=(150, 50)) as pilot:
        await pilot.pause()
        app._select_step("tools")
        await pilot.pause()
        tab = _tab(app, "ToolsTab")
        tab.refresh_view()
        table = tab.query_one(DataTable)  # the roles of every flow are listed at once, none configured or checked yet
        assert table.row_count >= 15 and {str(table.get_cell_at((i, 1))) for i in range(table.row_count)} == {"–"} and {str(table.get_cell_at((i, 4))) for i in range(table.row_count)} == {"not configured"}
        toolmod.write_preset(proj.config_dir)  # the user writes config/tools.json: listed as configured without running anything
        await pilot.pause()
        tab.refresh_view()
        assert "not configured" not in {str(tab.query_one(DataTable).get_cell_at((i, 4))) for i in range(tab.query_one(DataTable).row_count)}
        tab.action_check()
        for _ in range(60):
            await pilot.pause(0.25)
            if (proj.state_dir / "project" / "tool_rows.json").is_file():
                break
        await pilot.pause(0.5)  # (no manual refresh: the check redraws the tab itself, from the thread's result)
        table = tab.query_one(DataTable)
        rows = {table.coordinate_to_cell_key((i, 0)).row_key.value: [str(table.get_cell_at((i, c))) for c in range(5)] for i in range(table.row_count)}
        assert rows["fpga_synth"][2] == "✔" and rows["asic_synth"][2] == "✘"  # (the stand-in finds fpga_synth only)
        tab.action_modules()
        await pilot.pause()
        assert isinstance(app.screen, EditScreen)
        app.screen.query_one("#f-modules", Input).value = "oss-cad-suite"
        app.screen.action_save()
        await pilot.pause()
    assert proj.cfg.tools.modules == ["oss-cad-suite"]


async def test_the_flow_map_has_the_project_settings_box(proj):
    from q3tui.tui.flowmap import FlowMapScreen, Group

    app = Q3TUIApp(proj)
    async with app.run_test(size=(150, 80)) as pilot:
        await pilot.pause()
        app.action_switch_flow()
        await pilot.pause()
        assert isinstance(app.screen, FlowMapScreen)
        groups = {str(g.border_title).split("  ·")[0]: g for g in app.screen.query(Group)}
        assert groups["Project settings"].developed and groups["Project settings"].has_class("current")
        app.screen.pick(groups["ASIC synthesis"])
        await pilot.pause()
    assert app.return_value == ("flow", "asic")


# -- adding a tool: the path only, the LLM fills in the rest ---------------------------------------------------------------------------------------


@pytest.fixture
def tooldir(tmp_path):
    bindir = tmp_path / "vendor" / "sim" / "1.2" / "bin"
    bindir.mkdir(parents=True)
    for n in ("vsim", "vlog", "vcom"):
        (bindir / n).write_text("#!/bin/sh\n")
        (bindir / n).chmod(0o755)
    (bindir / "README.txt").write_text("docs, not executable")
    (bindir.parent / "README").write_text("VendorSim 1.2: vlog compiles, vsim simulates (batch: -c -do script.do).")
    return tmp_path / "vendor" / "sim" / "1.2"


class ToolLLM(GuessLLM):
    pass


def _one(**kw):
    g = dict(role="Sim", cmd="vsim -c -do {script}", parse="vcs", modules=["vendor/sim/1.2", "not/a/module"], description="Simulate with VendorSim", notes="vsim in batch mode; -do takes the script.")
    g.update(kw)
    return common.ToolGuess(**g)


def _tool_guess(*more, **kw):
    """The stage's answer: the default tool (changed by `kw`), then any more entries."""
    return common.ToolGuesses(tools=[_one(**kw), *more])


def test_scanning_a_tool(tooldir):
    found = common.tool_scan(str(tooldir))
    assert [Path(e).name for e in found["executables"]] == ["vcom", "vlog", "vsim"] and found["root"] == str(tooldir)  # (README.txt is not executable)
    assert common.tool_scan(str(tooldir / "bin" / "vsim"))["executables"] == [str(tooldir / "bin" / "vsim")]
    for bad, msg in (("", "give the path"), (str(tooldir / "nope"), "does not exist"), (str(tooldir / "bin" / "README.txt"), "not executable")):
        with pytest.raises(ValueError, match=msg):
            common.tool_scan(bad)
    (tooldir / "empty").mkdir()
    with pytest.raises(ValueError, match="no executable"):
        common.tool_scan(str(tooldir / "empty"))


def test_the_llm_sets_up_the_tool_and_code_checks_it(proj, tooldir, monkeypatch):
    from q3tui.llm import runtime

    monkeypatch.setattr(common, "modules_available", lambda timeout=20.0: ["synopsys/syn/V-2023.12", "vendor/sim/1.2"])
    fake = GuessLLM(_tool_guess())
    monkeypatch.setattr(runtime, "run_stage", fake)
    (g,) = common.identify_tool(Ops(Engine(proj)), str(tooldir))
    (stage,) = fake.stages
    assert stage.name == "tool_identify" and set(stage.builtin_tools) == {"Read", "Grep", "Glob"} and stage.cwd == tooldir  # read only, the tool's folder
    assert "You set up the tools found at a path for Q3TUI's flows" in stage.system_prompt and str(tooldir / "bin" / "vsim") in stage.prompt
    assert "- `sim`" in stage.prompt and "- `run`" in stage.prompt and "{tc}" in stage.prompt  # the roles that exist, with the placeholders their steps pass
    assert "vendor/sim/1.2" in stage.prompt and "vivado" in stage.prompt  # the modules, the log readers
    assert (g["role"], g["cmd"], g["parse"], g["modules"]) == ("sim", "vsim -c -do {script}", "vcs", ["vendor/sim/1.2"])  # the unknown module is dropped
    assert any("not/a/module" in w for w in g["warnings"]) and any("which the steps do not pass to 'sim'" in w and "{script}" in w for w in g["warnings"])


def test_a_roles_own_log_patterns_are_kept_when_the_llm_says_generic(proj, tmp_path, monkeypatch):
    from q3tui.llm import runtime

    fm = tmp_path / "fm" / "bin"
    fm.mkdir(parents=True)
    (fm / "fm_shell").write_text("#!/bin/sh\n")
    (fm / "fm_shell").chmod(0o755)
    monkeypatch.setattr(common, "modules_available", lambda timeout=20.0: ["synopsys/formality/V-2023.12"])
    monkeypatch.setattr(runtime, "run_stage", GuessLLM(_tool_guess(role="asic_lec", cmd="fm_shell -f {script}", parse="generic", modules=["synopsys/formality/V-2023.12"])))
    (g,) = common.identify_tool(Ops(Engine(proj)), str(fm.parent))
    assert g["role"] == "asic_lec" and g["parse"] == "" and g["warnings"] == []  # asic_lec reads its log with its own error / warning patterns: "generic" would lose them
    common.set_role(proj, "asic_lec", cmd=g["cmd"], modules=" ".join(g["modules"]), parse=g["parse"])
    r = json.loads((proj.root / "config" / "tools.json").read_text())["roles"]["asic_lec"]
    assert isinstance(r["parse"], dict) and r["parse"]["error"]  # (still the patterns)


def test_what_the_llm_got_wrong_about_a_tool_is_said(proj, tooldir, monkeypatch):
    from q3tui.llm import runtime

    monkeypatch.setattr(common, "modules_available", lambda timeout=20.0: [])
    monkeypatch.setattr(runtime, "run_stage", GuessLLM(_tool_guess(role="Brand New", cmd="mystery -x", parse="martian", modules=[])))
    (g,) = common.identify_tool(Ops(Engine(proj)), str(tooldir))
    assert g["role"] == "brand_new" and g["parse"] == "generic"
    assert [w.split(" ")[0] for w in g["warnings"]] == ["'brand_new'", "the", "log"] and any("not among the executables found" in w for w in g["warnings"])
    monkeypatch.setattr(runtime, "run_stage", GuessLLM(_tool_guess(cmd="  ")))
    with pytest.raises(ValueError, match="no tool it could set up"):
        common.identify_tool(Ops(Engine(proj)), str(tooldir))


def test_tool_add_with_only_a_path(proj, tooldir, monkeypatch):
    from q3tui.llm import runtime

    monkeypatch.setattr(common, "modules_available", lambda timeout=20.0: ["vendor/sim/1.2"])
    fake = GuessLLM(_tool_guess(cmd="vsim -c -do {tc}"))
    monkeypatch.setattr(runtime, "run_stage", fake)
    ops = Ops(Engine(proj))
    shown = ops.run_action("tool_identify", path=str(tooldir))
    assert shown.startswith("1 tool(s) found") and "role sim | vsim -c -do {tc}" in shown and "log reader vcs" in shown and "! module(s) not available here, dropped: not/a/module" in shown
    assert not (proj.root / "config" / "tools.json").exists()  # (identify writes nothing)
    msg = ops.run_action("tool_add", path=str(tooldir), parse="verilator")  # what you give wins
    assert msg.startswith("tool role 'sim' saved to tools.json") and "vsim in batch mode" in msg
    r = json.loads((proj.root / "config" / "tools.json").read_text())["roles"]["sim"]
    assert r["cmd"] == "vsim -c -do {tc}" and r["parse"] == "verilator" and r["modules"] == ["vendor/sim/1.2"] and r["description"] == "Simulate with VendorSim"
    assert {"lint", "synth", "run"} <= set(json.loads((proj.root / "config" / "tools.json").read_text())["roles"])  # the other roles stay
    n = len(fake.stages)
    ops.run_action("tool_add", path=str(tooldir), role="sim", cmd="vsim -c {tc} -l log")  # role and command: no LLM
    assert len(fake.stages) == n


async def test_the_tools_view_adds_a_tool_with_only_a_path(proj, tooldir, monkeypatch):
    from textual.widgets import Button, Input, Select

    from q3tui.llm import runtime
    from q3tui.tui.screens import EditScreen, PickToolsScreen

    monkeypatch.setattr(common, "modules_available", lambda timeout=20.0: ["vendor/sim/1.2"])
    fake = GuessLLM(_tool_guess(cmd="vsim -c -do {tc}"))
    monkeypatch.setattr(runtime, "run_stage", fake)
    mine = Project.open(proj.root)
    app = Q3TUIApp(mine)
    async with app.run_test(size=(160, 60)) as pilot:
        await pilot.pause()
        app._select_step("tools")
        await pilot.pause()
        tab = _tab(app, "ToolsTab")
        tab.action_add()
        await pilot.pause()
        assert isinstance(app.screen, EditScreen) and [f[0] for f in app.screen.fields] == ["path"] and [str(b.label) for b in app.screen.query(Button)] == ["Save (ctrl+s)", "Cancel (esc)"]
        app.screen.query_one("#f-path", Input).value = str(tooldir)
        app.screen.action_save()
        for _ in range(40):
            await pilot.pause(0.25)
            if isinstance(app.screen, PickToolsScreen):
                break
        scr = app.screen
        assert isinstance(scr, PickToolsScreen) and len(fake.stages) == 1 and "1 tool(s) found" in scr.title_text
        assert scr.items[0][0] == "sim  —  vsim -c -do {tc}" and "vsim in batch mode" in scr.items[0][1] and "not/a/module" in scr.items[0][1] and "log reader: vcs" in scr.items[0][1]
        scr.action_add()  # everything is checked
        await pilot.pause()
    r = json.loads((proj.root / "config" / "tools.json").read_text())["roles"]["sim"]
    assert r["cmd"] == "vsim -c -do {tc}" and r["parse"] == "vcs" and r["modules"] == ["vendor/sim/1.2"]


async def test_the_tools_dialog_lists_every_tool_found_and_adds_the_checked(proj, tooldir, monkeypatch):
    from textual.widgets import Input, SelectionList

    from q3tui.llm import runtime
    from q3tui.tui.screens import PickToolsScreen

    monkeypatch.setattr(common, "modules_available", lambda timeout=20.0: [])
    monkeypatch.setattr(runtime, "run_stage", GuessLLM(_tool_guess(common.ToolGuess(role="lint", cmd=f"{tooldir}/bin/vlog -lint {{filelist}}", description="lint"), cmd="vsim -c -do {tc}")))
    app = Q3TUIApp(Project.open(proj.root))
    async with app.run_test(size=(160, 60)) as pilot:
        await pilot.pause()
        app._select_step("tools")
        await pilot.pause()
        tab = _tab(app, "ToolsTab")
        tab.action_add()
        await pilot.pause()
        app.screen.query_one("#f-path", Input).value = str(tooldir)
        app.screen.action_save()
        for _ in range(40):
            await pilot.pause(0.25)
            if isinstance(app.screen, PickToolsScreen):
                break
        scr = app.screen
        assert isinstance(scr, PickToolsScreen) and [i[0].split("  ")[0] for i in scr.items] == ["sim", "lint"] and len(scr.query_one(SelectionList).selected) == 2  # all checked
        scr.query_one(SelectionList).deselect(0)  # leave sim out
        scr.action_add()
        await pilot.pause()
    roles = json.loads((proj.root / "config" / "tools.json").read_text())["roles"]
    assert roles["lint"]["cmd"].endswith("vlog -lint {filelist}") and roles["lint"]["description"] == "lint" and "vendor" not in roles["sim"]["cmd"]  # (sim stays the preset's)


async def test_a_tool_the_llm_cannot_identify_opens_the_form_by_hand(proj, tooldir, monkeypatch):
    from textual.widgets import Input

    from q3tui.llm import runtime
    from q3tui.tui.screens import EditScreen

    monkeypatch.setattr(runtime, "run_stage", GuessLLM(fail=runtime.StageError("no credentials")))
    app = Q3TUIApp(proj)
    async with app.run_test(size=(160, 60)) as pilot:
        await pilot.pause()
        app._select_step("tools")
        await pilot.pause()
        tab = _tab(app, "ToolsTab")
        tab.action_add()
        await pilot.pause()
        app.screen.query_one("#f-path", Input).value = str(tooldir)
        app.screen.action_save()
        for _ in range(40):
            await pilot.pause(0.25)
            if isinstance(app.screen, EditScreen) and "role" in [f[0] for f in app.screen.fields]:
                break
        assert isinstance(app.screen, EditScreen) and app.screen.title_text == "Add a tool (by hand)" and "Not identified" in app.screen.hint
        assert app.screen.query_one("#f-role", Input).value == "" and app.screen.query_one("#f-cmd", Input).value == str(tooldir)
        app.screen.dismiss(None)


# -- a folder with many tools: every one is found ----------------------------------------------------------------------------------------------------


def test_a_folder_with_many_tools_gives_one_entry_per_tool(proj, tooldir, monkeypatch):
    from q3tui.llm import runtime

    monkeypatch.setattr(common, "modules_available", lambda timeout=20.0: ["vendor/sim/1.2"])
    two = _tool_guess(common.ToolGuess(role="lint", cmd=f"{tooldir}/bin/vlog -lint {{filelist}}", parse="generic", description="lint"),
                      _one(role="sim", cmd="vcom {script}"), cmd="vsim -c -do {tc}")  # (the second 'sim' is a duplicate: dropped, said)
    fake = GuessLLM(two)
    monkeypatch.setattr(runtime, "run_stage", fake)
    found = common.identify_tool(Ops(Engine(proj)), str(tooldir))
    assert [g["role"] for g in found] == ["sim", "lint"] and any("another tool was proposed for 'sim'" in w for w in found[0]["warnings"])
    assert "one entry per role" in fake.stages[0].prompt and fake.stages[0].max_turns >= 30
    shown = Ops(Engine(proj)).run_action("tool_identify", path=str(tooldir))
    assert shown.startswith("2 tool(s) found") and "role lint" in shown
    msg = Ops(Engine(proj)).run_action("tool_add", path=str(tooldir))  # no role given: every tool found is saved
    roles = json.loads((proj.root / "config" / "tools.json").read_text())["roles"]
    assert "'sim' saved" in msg and "'lint' saved" in msg and roles["sim"]["cmd"] == "vsim -c -do {tc}" and roles["lint"]["cmd"].endswith("vlog -lint {filelist}")


# -- fpga: the devices the FPGA flow can synthesize for -----------------------------------------------------------------------------------------


def test_an_fpga_part_gets_its_family_from_its_name_and_the_specs_part_is_shown(proj):
    from q3tui.flows.fpga.lib import devices

    assert devices.identify("xc7a100tcsg324-1")["template"] == "xilinx7" and devices.identify("LFE5UM5G-85F-8BG381C")["device"] == "um5g-85k"
    assert devices.identify("iCE40UP5K-SG48")["device"] == "up5k" and devices.identify("10AXF40GAA")["template"] == "arria10"
    assert devices.identify("xczu9eg-ffvb1156-2-e")["template"] is None and "UltraScale" in devices.identify("xczu9eg-ffvb1156-2-e")["note"]
    (proj.root / "spec").mkdir()
    (proj.root / "spec" / "spec.md").write_text("| Technology | Intel Arria 10 1150, part 10AXF40GAA (DEP-003) | Quartus |\n")
    assert devices.spec_parts(proj) == ["10AXF40GAA"]
    e = {x["part"]: x for x in devices.entries(proj)}
    assert e["10AXF40GAA"]["sources"] == ["spec"] and e["10AXF40GAA"]["supported"] and e["10AXF40GAA"]["family"] == "Arria 10"
    assert e["10AX115N2F40E2LG"]["template"] == "arria10" and e["5CEBA4F23C7"]["template"] == "cyclonev"       # (the Intel families' own parts)
    assert e["xc7k325tffg900-2"]["template"] == "xilinx7" and e["85k"]["template"] == "ecp5" and e["up5k"]["template"] == "ice40"
    probs = devices.problems(proj)
    assert any("the spec names 10AXF40GAA" in p and "uses the default xc7k325tffg900-2" in p for p in probs)


def test_the_fpga_actions_add_use_and_remove_a_part(proj):
    from q3tui.flows.fpga.lib import devices

    ops = Ops(Engine(proj))
    assert "(Xilinx 7-series)" in ops.run_action("fpga_add", part="xc7a75tfgg484-1", title="my board")
    assert json.loads((proj.config_dir / "fpgas.json").read_text())["parts"] == [{"part": "xc7a75tfgg484-1", "title": "my board"}]
    assert "xc7a75tfgg484-1 | my board | Xilinx 7-series | yours" in ops.run_action("fpga_list")
    assert "saved to q3tui.yaml" in ops.run_action("fpga_use", part="LFE5U-85F")  # (a part from its name: ECP5's device flag)
    use = devices.in_use(Project.open(proj.root))
    assert (use["template"], use["device"], use["set"]) == ("ecp5", "85k", True) and "fpga_synth" in (proj.root / "q3tui.yaml").read_text()
    ops.run_action("fpga_use", part="10AX115N2F40E2LG")                                    # an Intel part: Quartus
    assert (devices.in_use(Project.open(proj.root))["template"]) == "arria10"
    with pytest.raises(EngineError, match="no family here synthesizes Xilinx UltraScale"):
        ops.run_action("fpga_use", part="xczu9eg-ffvb1156-2-e")
    ops.run_action("fpga_use", part="xc7a75tfgg484-1")
    assert devices.in_use(Project.open(proj.root))["device"] == "xc7a75tfgg484-1"
    ops.run_action("fpga_remove", part="xc7a75tfgg484-1")
    with pytest.raises(EngineError, match="not in config/fpgas.json"):
        ops.run_action("fpga_remove", part="xc7a75tfgg484-1")


async def test_the_fpga_view_lists_the_devices_and_u_uses_one(proj):
    from textual.widgets import DataTable

    from q3tui.flows.fpga.lib import devices

    mine = Project.open(proj.root)
    app = Q3TUIApp(mine)
    async with app.run_test(size=(160, 60)) as pilot:
        await pilot.pause()
        app._select_step("fpga")
        await pilot.pause()
        tab = _tab(app, "FpgaTab")
        tab.refresh_view()
        table = tab.query_one(DataTable)
        keys = [table.coordinate_to_cell_key((i, 0)).row_key.value for i in range(table.row_count)]
        assert {"xc7k325tffg900-2", "85k", "up5k"} <= set(keys)
        table.move_cursor(row=keys.index("xc7a100tcsg324-1"))
        await pilot.pause()
        tab.action_use()
        await pilot.pause()
    assert devices.in_use(Project.open(proj.root))["device"] == "xc7a100tcsg324-1"   # saved to q3tui.yaml
