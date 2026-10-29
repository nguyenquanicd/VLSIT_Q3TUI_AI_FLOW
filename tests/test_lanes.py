"""Lanes (core/lanes.py, docs/spec/lanes.md): tags, lane folders over a tag (hard links, copy on write), the lanes at the same time, what a lane
changes, the merge, the convergence loop. Code-only step kinds: a design flow (spec → rtl, each applying its change requests to its file) and lane
flows that check the RTL and, when it lacks something, hand a fix to the design flow within the run (as the dv triage does), fix a file of their
own, ask you, or fail."""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

import anyio
import pytest

from q3tui import flows
from q3tui.core import lanes
from q3tui.core.project import Project
from q3tui.pipeline.base import StepContext, StepDef, StepFailed
from q3tui.pipeline.engine import Engine, EngineError


# -- the step kinds -------------------------------------------------------------------------------------------------------------------


class TSpec(StepDef):
    """Design: spec/intent.md → spec/spec.md; a change request becomes a line of it."""

    title = "T spec"

    def inputs(self, engine):
        return [engine.project.root / "spec" / "intent.md"]

    def outputs(self, engine):
        p = engine.project.root / "spec" / "spec.md"
        return [p] if p.is_file() else []

    async def run(self, ctx: StepContext) -> None:
        p = ctx.project.root / "spec" / "spec.md"
        lines = p.read_text().splitlines() if p.is_file() else [(ctx.project.root / "spec" / "intent.md").read_text().strip()]
        p.write_text("\n".join([*lines, *(f"decision: {f}" for f in ctx.feedback)]) + "\n")


class TRtl(StepDef):
    """Design: src/rtl/top.sv from the spec; a change request `[rtl:top] X` becomes a `// fix: X` line (kept across runs)."""

    title = "T rtl"

    def inputs(self, engine):
        return [engine.project.root / "spec" / "spec.md"]

    def outputs(self, engine):
        p = engine.project.root / "src" / "rtl" / "top.sv"
        return [p] if p.is_file() else []

    async def run(self, ctx: StepContext) -> None:
        p = ctx.project.root / "src" / "rtl" / "top.sv"
        p.parent.mkdir(parents=True, exist_ok=True)
        fixes = [x for x in p.read_text().splitlines() if x.startswith("// fix:")] if p.is_file() else []
        fixes += [f"// fix: {f.split('] ', 1)[-1]}" for f in ctx.feedback if f"// fix: {f.split('] ', 1)[-1]}" not in fixes]
        p.write_text("module top;\n" + "".join(f"{x}\n" for x in fixes) + "endmodule\n")


class TTb(StepDef):
    """A lane's own file: src/tb/<name>.txt; a change request becomes a line of it."""

    title = "T tb"

    def inputs(self, engine):
        return [engine.project.root / "spec" / "spec.md"]

    def outputs(self, engine):
        p = engine.project.root / "src" / "tb" / f"{self.name}.txt"
        return [p] if p.is_file() else []

    async def run(self, ctx: StepContext) -> None:
        p = ctx.project.root / "src" / "tb" / f"{self.name}.txt"
        p.parent.mkdir(parents=True, exist_ok=True)
        lines = p.read_text().splitlines() if p.is_file() else [f"tb {self.options.get('text', '')}"]
        p.write_text("\n".join([*lines, *ctx.feedback]) + "\n")


class TCheck(StepDef):
    """A lane's check of the RTL (and of its own tb file). options: need (a fix the RTL must have), tb (its own tb step), mode (fix: hand the
    fix to the design flow within the run; ask: a blocking question; fail; again: a new fix on every tag), sleep, stamps (a file for its times)."""

    title = "T check"

    def inputs(self, engine):
        tb = self.options.get("tb")
        again = [engine.project.state_dir / lanes.LANE_FILE] if self.options.get("mode") == "again" else []  # (stale on every new tag)
        return [engine.project.root / "src" / "rtl" / "top.sv", *([engine.project.root / "src" / "tb" / f"{tb}.txt"] if tb else []), *again]

    def outputs(self, engine):
        p = engine.project.root / "results" / f"{self.name}.txt"
        return [p] if p.is_file() else []

    def question_list(self, engine):
        p = engine.project.root / "results" / f"{self.name}.question"
        return [{"id": "Q1", "question": p.read_text(), "blocking": True, "default_assumption": ""}] if p.is_file() else []

    async def run(self, ctx: StepContext) -> None:
        o, root, eng = self.options, ctx.project.root, ctx.engine
        t0 = time.monotonic()
        await anyio.sleep(float(o.get("sleep", 0)))
        if o.get("stamps"):
            with open(o["stamps"], "a") as f:
                f.write(f"{self.name} {t0} {time.monotonic()}\n")
        meta = eng.state.step_meta.setdefault(self.name, {})
        rounds = int(meta.get("rounds", 0))
        rtl = (root / "src" / "rtl" / "top.sv").read_text()
        tb = o.get("tb")
        if tb and "tb-ok" not in (root / "src" / "tb" / f"{tb}.txt").read_text():  # its own file is wrong: its own step fixes it
            if rounds >= 3:
                raise StepFailed("tb never fixed")
            eng.request_change(tb, "tb-ok")
            meta["loop"], meta["rounds"] = True, rounds + 1
            return
        need = o.get("need")
        if o.get("mode") == "again":  # (a new fix wanted on every tag: never passes on one)
            tag = (lanes.lane_info(ctx.project) or {}).get("tag", "")
            need = None if meta.get("done") == tag else f"N-{tag}"
            meta["done"] = tag
        if need and f"// fix: {need}" not in rtl:
            mode = o.get("mode", "fix")
            if mode == "fail":
                raise StepFailed(f"the RTL lacks {need}")
            if mode == "ask":
                (root / "results").mkdir(exist_ok=True)
                (root / "results" / f"{self.name}.question").write_text(f"Should the RTL have {need}?")
                raise StepFailed("waiting for your answer")
            if rounds >= 3:
                raise StepFailed(f"the RTL still lacks {need}")
            eng.save()  # (as the dv triage: the design flow's engine queues the fix, this one reads it back, the run runs that flow)
            other = Engine(ctx.project, flow=flows.load_flow("design", root))
            other.request_change("rtl", f"[rtl:top] {need}")
            eng.reload()
            eng.hand_to_flow(self.name, "design")
            eng.state.step_meta[self.name]["rounds"] = rounds + 1
            return
        (root / "results").mkdir(exist_ok=True)
        (root / "results" / f"{self.name}.txt").write_text(f"pass on {rtl.count('// fix:')} fix(es)\n")


@pytest.fixture(autouse=True)
def _kinds():
    from q3tui.steps import KINDS, register_kind

    for k in ("t_spec", "t_rtl", "t_tb", "t_check"):
        register_kind(k, f"tests.test_lanes:{ {'t_spec': 'TSpec', 't_rtl': 'TRtl', 't_tb': 'TTb', 't_check': 'TCheck'}[k] }")
    yield
    for k in ("t_spec", "t_rtl", "t_tb", "t_check"):
        KINDS.pop(k, None)


SPEC = "- {id: spec, kind: t_spec}\n"


def _flow(root: Path, name: str, steps: str) -> None:
    (root / "flows").mkdir(exist_ok=True)
    (root / "flows" / f"{name}.yaml").write_text(f"name: {name}\nsteps:\n" + "".join(f"  {s}\n" for s in steps.splitlines() if s.strip()))


def _project(tmp_path: Path, lane_flows=("la", "lb", "lc"), design=True) -> Project:
    root = tmp_path / "proj"
    (root / "spec").mkdir(parents=True)
    (root / "spec" / "intent.md").write_text("a block\n")
    stamps = tmp_path / "stamps.txt"
    _flow(root, "design", SPEC + "- {id: rtl, kind: t_rtl, deps: [spec]}")
    _flow(root, "la", SPEC + "- {id: tb_a, kind: t_tb, deps: [spec]}\n"
                       f"- {{id: check_a, kind: t_check, deps: [tb_a], options: {{need: A, tb: tb_a, sleep: 0.3, stamps: '{stamps}'}}}}")
    _flow(root, "lb", SPEC + f"- {{id: check_b, kind: t_check, deps: [spec], options: {{need: B, sleep: 0.3, stamps: '{stamps}'}}}}")
    _flow(root, "lc", SPEC + f"- {{id: check_c, kind: t_check, deps: [spec], options: {{sleep: 0.3, stamps: '{stamps}'}}}}")
    _flow(root, "lf", SPEC + "- {id: check_f, kind: t_check, deps: [spec], options: {need: Z, mode: fail}}")
    _flow(root, "lq", SPEC + "- {id: check_q, kind: t_check, deps: [spec], options: {need: Q, mode: ask}}")
    _flow(root, "lg", SPEC + "- {id: check_g, kind: t_check, deps: [spec], options: {mode: again}}")
    _flow(root, "lx", SPEC + "- {id: tbx, kind: t_tb, deps: [spec], options: {text: x}}")
    _flow(root, "ly", SPEC + "- {id: tbx, kind: t_tb, deps: [spec], options: {text: y}}")
    (root / "q3tui.yaml").write_text("pipeline: {flow: design}\n"
                                     f"lanes: {{flows: [{', '.join(lane_flows)}], design_flows: [design]}}\n")
    project = Project.open(root)
    if design:
        assert anyio.run(lambda: Engine(project, flow=flows.load_flow("design", root)).run(yes=True)) == "complete"
    return project


def _ino(p: Path) -> int:
    return p.stat().st_ino


# -- tags -------------------------------------------------------------------------------------------------------------------------------


def test_a_tag_is_a_read_only_copy_of_the_finished_design(tmp_path):
    project = _project(tmp_path, design=False)
    with pytest.raises(EngineError, match="the design is not finished"):
        lanes.make_tag(project)
    forced = lanes.make_tag(project, "draft", force=True)
    assert forced["forced"] and "design:spec" in forced["forced"][0]
    anyio.run(lambda: Engine(project, flow=flows.load_flow("design", project.root)).run(yes=True))
    info = lanes.make_tag(project)
    assert info["name"] == "v2" and info["previous"] == "draft" and not info["forced"]
    files = lanes.tags_dir(project) / "v2" / "files"
    top = files / "src" / "rtl" / "top.sv"
    assert top.read_text() == "module top;\nendmodule\n" and (files / "flows" / "la.yaml").is_file() and (files / "q3tui.yaml").is_file()
    assert not top.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)       # frozen
    assert (lanes.tags_dir(project) / "v2" / "pipeline.json").is_file()
    assert not (files / ".q3tui").exists() and [t["name"] for t in lanes.list_tags(project)] == ["draft", "v2"]
    with pytest.raises(EngineError, match="exists"):
        lanes.make_tag(project, "v2")


def test_a_configuration_that_names_the_project_folder_is_refused(tmp_path):
    project = _project(tmp_path)
    (project.root / "config").mkdir()
    (project.root / "config" / "tools.json").write_text(json.dumps({"roles": {"run": {"cmd": f"{project.root}/simv"}}}, indent=1))
    with pytest.raises(EngineError, match=r"names the project folder itself \(config/tools.json:"):
        lanes.make_tag(project)


# -- lane folders --------------------------------------------------------------------------------------------------------------------------


def test_a_lane_folder_links_to_the_tag_and_copies_only_what_a_step_writes(tmp_path):
    project = _project(tmp_path)
    lanes.make_tag(project)
    d = lanes.make_lane(project, "v1", "lc")
    tag = lanes.tags_dir(project) / "v1" / "files"
    assert d == project.root.parent / "proj.lanes" / "v1" / "lc"                        # beside the project folder
    assert _ino(d / "spec" / "spec.md") == _ino(tag / "spec" / "spec.md")               # the design: linked
    assert _ino(d / "flows" / "lc.yaml") != _ino(tag / "flows" / "lc.yaml")             # the small folders: copied, writable
    assert os.access(d / "flows" / "lc.yaml", os.W_OK)
    assert lanes.lane_info(Project.open(d))["tag"] == "v1" and lanes.lane_info(project) is None
    if os.geteuid() != 0:
        with pytest.raises(PermissionError):                                             # a write that was not declared: it fails
            (d / "spec" / "spec.md").write_text("changed")
    lp = Project.open(d)
    assert lanes.materialize(lp, [d / "spec" / "spec.md"]) == 2                          # spec.md and intent.md: the folder
    (d / "spec" / "spec.md").write_text("changed in the lane\n")
    assert (tag / "spec" / "spec.md").read_text() == "a block\n" and _ino(d / "spec" / "spec.md") != _ino(tag / "spec" / "spec.md")


def test_only_a_lane_s_own_requests_to_the_design_are_recorded(tmp_path):
    project = _project(tmp_path)
    lanes.make_tag(project)
    lp = Project.open(lanes.make_lane(project, "v1", "lb"))
    lanes.note_request(lp, "rtl", "[rtl:top] B", "check_b")                             # the lane's check → the design: recorded
    lanes.note_request(lp, "spec", "[decision] keep it", "rtl")                         # the design to itself: its own run makes it again
    lanes.note_request(lp, "check_b", "look again", "check_b")                          # its own step: not a design change
    lanes.note_request(project, "rtl", "x", None)                                       # the project folder: not a lane
    assert [(c["target"], c["origin"]) for c in lanes.read_changes(lp.root)] == [("rtl", "check_b")]
    assert lanes.read_changes(project.root) == []


# -- lanes ---------------------------------------------------------------------------------------------------------------------------------


def test_the_lanes_run_at_the_same_time_and_each_fixes_in_its_own_folder(tmp_path):
    project = _project(tmp_path)
    lanes.make_tag(project)
    res = anyio.run(lambda: lanes.run_lanes(project))
    assert {n: r["status"] for n, r in res.items()} == {"la": "fixed", "lb": "fixed", "lc": "passed"}
    stamps = [ln.split() for ln in (tmp_path / "stamps.txt").read_text().splitlines()]
    first = {}
    for name, t0, t1 in stamps:
        first.setdefault(name, (float(t0), float(t1)))
    (b0, b1), (c0, c1) = first["check_b"], first["check_c"]
    assert max(b0, c0) < min(b1, c1)                                                     # overlapping: at the same time
    la = lanes.lane_dir(project, "v1", "la")
    assert "// fix: A" in (la / "src" / "rtl" / "top.sv").read_text() and "tb-ok" in (la / "src" / "tb" / "tb_a.txt").read_text()
    assert (lanes.tags_dir(project) / "v1" / "files" / "src" / "rtl" / "top.sv").read_text() == "module top;\nendmodule\n"   # the tag: as it was
    assert (project.root / "src" / "rtl" / "top.sv").read_text() == "module top;\nendmodule\n"                                # your folder too
    changes = lanes.read_changes(la)
    assert [(c["kind"], c.get("target") or c.get("step")) for c in changes] == [("own", "tb_a"), ("request", "rtl")]
    assert res["la"]["requests"] == 1 and res["la"]["own_fixes"] == ["tb_a"]
    assert lanes.tag_results(project, "v1")["lc"]["status"] == "passed" and "v1" in lanes.status_text(project)


def test_a_lane_that_cannot_fix_fails_and_one_that_asks_waits_for_you(tmp_path):
    project = _project(tmp_path, lane_flows=("lf", "lq"))
    lanes.make_tag(project)
    res = anyio.run(lambda: lanes.run_lanes(project))
    assert res["lf"]["status"] == "failed" and "lacks Z" in res["lf"]["detail"]
    assert res["lq"]["status"] == "needs_you" and "Q1" in res["lq"]["detail"] and "answer" in res["lq"]["detail"]
    with pytest.raises(EngineError, match="nothing to merge"):
        anyio.run(lambda: lanes.merge(project))


# -- merge, converge -----------------------------------------------------------------------------------------------------------------------


def test_the_merge_replays_the_lanes_fixes_and_takes_their_own_steps(tmp_path):
    project = _project(tmp_path)
    lanes.make_tag(project)
    anyio.run(lambda: lanes.run_lanes(project))
    plan = lanes.merge_plan(project, "v1")
    assert plan.lanes == ["la", "lb"] and [r["feedback"] for r in plan.requests] == ["[rtl:top] A", "[rtl:top] B"]
    assert plan.overlaps == ["rtl:top: la, lb"] and not plan.conflicts                  # both changed the module: both requests go to it
    assert plan.own == {"tb_a": "la", "check_a": "la", "check_b": "lb", "check_c": "lc"}  # (the passed lane's result too)
    m = anyio.run(lambda: lanes.merge(project, "v1"))
    assert m["merged"] and m["tag"] == "v2" and m["from"] == "v1"
    top = (project.root / "src" / "rtl" / "top.sv").read_text()
    assert "// fix: A" in top and "// fix: B" in top                                     # replayed: the owning step applied both
    assert "tb-ok" in (project.root / "src" / "tb" / "tb_a.txt").read_text()            # the lane's own file, as it left it
    st = Engine(project).state
    assert st.steps["tb_a"].status == "done" and "check_c" in st.steps
    assert (lanes.tags_dir(project) / "v2" / "files" / "src" / "rtl" / "top.sv").read_text() == top


def test_two_lanes_that_left_one_own_step_different_are_a_conflict(tmp_path):
    project = _project(tmp_path, lane_flows=("lx", "ly"))
    lanes.make_tag(project)
    anyio.run(lambda: lanes.run_lanes(project))
    assert lanes.merge_plan(project, "v1").conflicts == ["tbx: lx, ly"]
    with pytest.raises(EngineError, match="more than one lane changed the same step"):
        anyio.run(lambda: lanes.merge(project, "v1"))
    plan = lanes.merge_plan(project, "v1", ["lx"])                                       # you choose: that lane's only
    assert not plan.conflicts and plan.own == {"tbx": "lx"}


def test_converge_until_every_lane_passes_on_one_tag(tmp_path):
    project = _project(tmp_path)
    res = anyio.run(lambda: lanes.converge(project))
    assert res.ok and res.stopped == "converged" and res.converged == "v2"
    assert [(r.tag, r.results) for r in res.rounds] == [("v1", {"la": "fixed", "lb": "fixed", "lc": "passed"}),
                                                        ("v2", {"la": "passed", "lb": "passed", "lc": "passed"})]
    assert "merged la, lb → v2" in res.rounds[0].note and (lanes.tags_dir(project) / "v2" / "converged").is_file()
    assert "converged on v2" in lanes.converge_summary(res)


def test_converge_stops_when_a_round_is_not_better(tmp_path):
    project = _project(tmp_path, lane_flows=("lg",))
    res = anyio.run(lambda: lanes.converge(project, max_rounds=5))
    assert not res.ok and res.stopped == "no improvement" and len(res.rounds) == 2 and res.best_tag == "v1"
    res = anyio.run(lambda: lanes.converge(project, merge_ok=False))
    assert res.stopped.startswith("the merge of lg waits for you")


def test_clean_deletes_the_lane_folders_and_keeps_what_they_changed(tmp_path):
    project = _project(tmp_path, lane_flows=("lb",))
    lanes.make_tag(project)
    anyio.run(lambda: lanes.run_lanes(project))
    assert lanes.clean(project, "v1") == ["v1"]
    assert not lanes.lane_dir(project, "v1", "lb").exists()
    kept = lanes.tags_dir(project) / "v1" / "changes" / "lb.jsonl"
    assert kept.is_file() and "[rtl:top] B" in kept.read_text() and lanes.tag_results(project, "v1")["lb"]["status"] == "fixed"


def test_the_cli_tags_and_shows_the_lanes(tmp_path):
    from click.testing import CliRunner

    from q3tui.core.cli import main

    project = _project(tmp_path)
    r = CliRunner().invoke(main, ["-C", str(project.root), "tag"])
    assert r.exit_code == 0 and "tag v1" in r.output
    r = CliRunner().invoke(main, ["-C", str(project.root), "lanes", "status"])
    assert r.exit_code == 0 and r.output.startswith("v1")


# -- ops, assistant, TUI ---------------------------------------------------------------------------------------------------------------------


def test_the_lanes_are_user_actions_of_ops_and_the_assistant(tmp_path):
    from q3tui.core.ops import Ops
    from q3tui.llm.assistant import assistant_tools

    project = _project(tmp_path)
    ops = Ops(Engine(project))
    assert ops.tag().startswith("tag v1:") and ops.lanes_status().startswith("v1")
    anyio.run(lambda: lanes.run_lanes(project))
    assert ops.lanes_merge_preview().startswith("v1: requests of la, lb (2)") and "both changed rtl:top: la, lb" in ops.lanes_merge_preview()

    class Host:
        def __init__(self):
            self.engine, self.ops, self.started, self.asked = ops.engine, ops, [], []

        def start_lanes(self, args):
            self.started.append(args)
            return "started"

        async def confirm(self, question, detail=""):
            self.asked.append(question)
            return True

        def focus_step(self):
            return None

    host = Host()
    tools = {t.name: t for t in assistant_tools(host)}
    out = anyio.run(lambda: tools["lanes_status"].handler({}))["content"][0]["text"]
    assert "la" in out and "fixed" in out
    anyio.run(lambda: tools["run_lanes"].handler({"what": "merge"}))
    anyio.run(lambda: tools["run_lanes"].handler({"what": "run", "lanes": "la,lb"}))
    assert host.started == [["lanes", "merge", "--yes"], ["lanes", "run", "--lanes", "la,lb"]] and host.asked == ["Merge the lanes' fixes into the project folder?"]


async def test_the_lanes_screen_lists_the_tags_and_starts_the_lanes(tmp_path, monkeypatch):
    from textual.widgets import DataTable

    from q3tui.tui.app import Q3TUIApp
    from q3tui.tui.screens import LanesScreen

    project = await anyio.to_thread.run_sync(_project, tmp_path)  # (its design run has an event loop of its own)
    lanes.make_tag(project)
    await lanes.run_lanes(project)
    app = Q3TUIApp(project)
    started = []
    monkeypatch.setattr(app, "start_lanes", lambda args: started.append(args) or "started")
    async with app.run_test(size=(170, 50)) as pilot:
        await pilot.pause()
        app.action_lanes()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, LanesScreen)
        t = screen.query_one(DataTable)
        rows = {t.coordinate_to_cell_key((i, 0)).row_key.value: [str(t.get_cell_at((i, c))) for c in range(len(t.columns))] for i in range(t.row_count)}
        assert rows["v1/la"][1:3] == ["la", "fixed"] and rows["v1/lc"][2] == "passed"
        await pilot.press("r")
        await pilot.press("c")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert started == [["lanes", "run"], ["lanes", "converge", "--yes"]]
