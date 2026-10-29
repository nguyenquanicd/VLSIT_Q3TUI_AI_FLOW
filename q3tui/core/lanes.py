"""Lanes (docs/spec/lanes.md): the checking flows run at the same time, each in a folder of its own over a frozen design version (a tag), a
failing lane fixes in its folder, the fixes are merged into the next tag, every lane checks again, until all pass on one tag.

- A **tag** (`q3tui tag`) is a read-only copy of the design files (`.q3tui/tags/<name>/files/`) with the state of the steps that made it.
- A **lane** (`q3tui lanes run`) is a project folder of its own (`<project>.lanes/<tag>/<lane>/`, beside the project: inside `.q3tui/` its
  `.ignore` would hide the lane from the agents' searches): its own state file, lock and build output. The bulk of the design (spec/, src/) is
  hard-linked to the tag, the small folders copied. **Copy on write**: just before a step runs, the folders it writes become real copies
  (`before_step`); the tag's files are read-only, so a write that was not declared fails instead of changing the tag.
- What a lane changes in a step it does not own (a design step: the steps of `lanes.design_flows`) is recorded as the change request it made
  (`.q3tui/lane_changes.jsonl`), and so is every own step that ran with change requests (its files are taken as they are at the merge).
- The **merge** (`q3tui lanes merge`) replays the recorded change requests in the project folder (the owning steps apply both lanes' fixes knowing
  both), takes the lanes' own steps, runs the design flows and makes the next tag. **Converge** (`q3tui lanes converge`) repeats lanes → merge until
  every lane passes on one tag, bounded, stopping when a round is not better than the best one.
"""

from __future__ import annotations

import contextvars
import json
import os
import shutil
import stat
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from q3tui.core.project import Project, read_json, sha256_file, write_json

# the step running in this task (the engine sets it): who made a change request
CURRENT_STEP: contextvars.ContextVar[str | None] = contextvars.ContextVar("q3tui_current_step", default=None)

LINKED = ("spec_dir", "src_dir")  # hard-linked into a lane: the bulk of the design, copied only when a step writes there
COPIED = ("config_dir", "constraints_dir", "schemas_dir", "reviews_dir")  # small and written by many steps: copied
EXTRA = ("flows", "prompts", "q3tui.yaml")
LANE_FILE = "lane.json"
CHANGES = "lane_changes.jsonl"
STATUS_OK = ("passed",)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _top(project: Project, attr: str) -> str:
    return Path(getattr(project.cfg.project, attr)).parts[0]


def design_entries(project: Project) -> list[str]:
    """The top-level names a tag copies: what describes the design and its checks (not build/, reports/, .q3tui/)."""
    return list(dict.fromkeys([*(_top(project, a) for a in (*LINKED, *COPIED)), *EXTRA]))


def tags_dir(project: Project) -> Path:
    return project.state_dir / "tags"


def lanes_root(project: Project) -> Path:
    d = project.cfg.lanes.dir
    if d:
        p = Path(d).expanduser()
        return p if p.is_absolute() else project.root / p
    return project.root.parent / f"{project.root.name}.lanes"


def lane_dir(project: Project, tag: str, lane: str) -> Path:
    return lanes_root(project) / tag / lane


def list_tags(project: Project) -> list[dict]:
    out = [read_json(d / "tag.json", None) for d in tags_dir(project).glob("*/") if (d / "tag.json").is_file()] if tags_dir(project).is_dir() else []
    return sorted((t for t in out if t), key=lambda t: t.get("n", 0))


def latest_tag(project: Project) -> str | None:
    tags = list_tags(project)
    return tags[-1]["name"] if tags else None


def tag_results(project: Project, tag: str) -> dict[str, dict]:
    d = tags_dir(project) / tag / "results"
    return {f.stem: read_json(f, {}) for f in sorted(d.glob("*.json"))} if d.is_dir() else {}


def design_steps(project: Project) -> set[str]:
    """The steps of the design flows: what a tag freezes and only the merge changes in the project folder."""
    cached = getattr(project, "_design_steps", None)
    if cached is None:
        from q3tui import flows

        cached = set()
        for name in project.cfg.lanes.design_flows:
            try:
                cached |= {s.id for s in flows.load_flow(name, project.root).steps}
            except Exception:  # noqa: BLE001 - a design flow that is not there makes no design step
                continue
        project._design_steps = cached  # type: ignore[attr-defined]
    return cached


def lane_info(project: Project) -> dict | None:
    """The lane this project folder is ({tag, lane, main, tag_dir}), or None for a project folder of your own."""
    if not hasattr(project, "_lane_info"):
        project._lane_info = read_json(project.state_dir / LANE_FILE, None)  # type: ignore[attr-defined]
    return project._lane_info  # type: ignore[attr-defined]


# -- tags -------------------------------------------------------------------------------------------------------------


def design_problems(project: Project) -> list[str]:
    """The design steps that are not done and approved (a tag freezes a finished design)."""
    from q3tui import flows
    from q3tui.pipeline.engine import Engine

    out = []
    for name in project.cfg.lanes.design_flows:
        try:
            eng = Engine(project, flow=flows.load_flow(name, project.root))
        except Exception as exc:  # noqa: BLE001
            out.append(f"flow {name}: {exc}")
            continue
        for v in eng.status():
            if v.status == "unavailable":  # (a step this project cannot run: no tool for it)
                continue
            if v.status not in ("done", "user") or v.gate == "open":
                out.append(f"{name}:{v.name} {v.status.replace('_', ' ')}" + (" (review open)" if v.gate == "open" and v.status in ("done", "user") else ""))
    return list(dict.fromkeys(out))


def absolute_paths(project: Project) -> list[str]:
    """Places in the configuration that name the project folder itself: in a lane they would point at the wrong files."""
    root = str(project.root)
    out = []
    for f in (project.config_dir / "tools.json", project.root / "q3tui.yaml"):
        if f.is_file():
            for i, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
                if root in line:
                    out.append(f"{project.rel(f)}:{i}")
    return out


def _design_files(base: Path, entries: list[str]) -> dict[str, str]:
    out = {}
    for e in entries:
        p = base / e
        files = [p] if p.is_file() else sorted(x for x in p.rglob("*") if x.is_file()) if p.is_dir() else []
        for f in files:
            out[str(f.relative_to(base))] = sha256_file(f)
    return out


def design_changed_since(project: Project, tag: str) -> bool:
    """The project folder's design files differ from the tag's (you changed something since)."""
    entries = design_entries(project)
    return _design_files(project.root, entries) != _design_files(tags_dir(project) / tag / "files", entries)


def next_tag_name(project: Project) -> str:
    n = len(list_tags(project)) + 1
    while (tags_dir(project) / f"v{n}").exists():
        n += 1
    return f"v{n}"


def make_tag(project: Project, name: str | None = None, force: bool = False, fork: str | None = None) -> dict:
    """Freeze the design of the project folder: a read-only copy of its design files and the state of its steps. `fork`: the checking flow
    whose run forks here (`fork_run`): made while the other runs go on (they never change the design), its design steps waiting for the fork's
    change requests (not a problem: they are what the fork applies)."""
    from q3tui.pipeline.engine import Engine, EngineError

    if lane_info(project):
        raise EngineError("this is a lane folder: tags are made in the project folder")
    eng = Engine(project)
    other = next(iter(eng.other_runs()), None)
    if other and not fork:
        raise EngineError(f"a run is going on (pid {other['pid']}, {other['flow']}): tag when it ends")
    where = absolute_paths(project)
    if where:
        raise EngineError(f"the configuration names the project folder itself ({', '.join(where)}): a lane would read the wrong files; "
                          "use {project} / a relative path there")
    problems = design_problems(project)
    force = force or bool(fork)
    if problems and not force:
        raise EngineError("the design is not finished: " + "; ".join(problems[:8]) + (" …" if len(problems) > 8 else "")
                          + f" (run the {' / '.join(project.cfg.lanes.design_flows)} flow first, or tag anyway with --force)")
    name = name or next_tag_name(project)
    d = tags_dir(project) / name
    if d.exists():
        raise EngineError(f"tag {name} exists")
    files = d / "files"
    files.mkdir(parents=True)
    size = count = 0
    for e in design_entries(project):
        src = project.root / e
        if src.is_dir():
            shutil.copytree(src, files / e, symlinks=True, ignore=shutil.ignore_patterns("__pycache__"))
        elif src.is_file():
            shutil.copy2(src, files / e)
    for f in files.rglob("*"):
        if f.is_file() and not f.is_symlink():
            st = f.stat()
            os.chmod(f, st.st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))  # (frozen: a write through a lane's link fails)
            size, count = size + st.st_size, count + 1
    if eng.state_path.is_file():
        shutil.copy2(eng.state_path, d / "pipeline.json")
    info = {"name": name, "n": len(list_tags(project)) + 1, "at": _now(), "previous": latest_tag(project), "files": count, "bytes": size,
            "forced": problems if force and not fork else [], **({"fork": fork} if fork else {})}
    write_json(d / "tag.json", info)
    return info


# -- lane folders: links, copy on write --------------------------------------------------------------------------------


def _writable(p: Path) -> None:
    for f in ([p] if p.is_file() else [x for x in p.rglob("*") if x.is_file()]):
        os.chmod(f, f.stat().st_mode | stat.S_IWUSR)


def _link_tree(src: Path, dst: Path) -> None:
    for d, _dirs, names in os.walk(src):
        rel = Path(d).relative_to(src)
        (dst / rel).mkdir(parents=True, exist_ok=True)
        for n in names:
            s, t = Path(d) / n, dst / rel / n
            try:
                os.link(s, t)
            except OSError:  # (another file system: a plain copy, the lane's own)
                shutil.copy2(s, t)
                _writable(t)


def make_lane(project: Project, tag: str, lane: str) -> Path:
    """The lane's folder over the tag (made once: a lane run again on the same tag goes on in it)."""
    from q3tui.pipeline.engine import EngineError

    dest = lane_dir(project, tag, lane)
    if (dest / ".q3tui" / LANE_FILE).is_file():
        return dest
    t = tags_dir(project) / tag
    if not (t / "files").is_dir():
        raise EngineError(f"no tag '{tag}' (q3tui tag makes one)")
    dest.mkdir(parents=True, exist_ok=True)
    linked = {_top(project, a) for a in LINKED}
    for e in sorted((t / "files").iterdir()):
        if e.name in linked and e.is_dir():
            _link_tree(e, dest / e.name)
        elif e.is_dir():
            shutil.copytree(e, dest / e.name, symlinks=True)
            _writable(dest / e.name)
        else:
            shutil.copy2(e, dest / e.name)
            _writable(dest / e.name)
    lp = Project(dest, project.cfg)
    state = lp.ensure_state_dir()
    if (t / "pipeline.json").is_file():
        shutil.copy2(t / "pipeline.json", state / "pipeline.json")
        _writable(state / "pipeline.json")
    write_json(state / LANE_FILE, {"tag": tag, "lane": lane, "main": str(project.root), "tag_dir": str(t), "at": _now()})
    return dest


def _shared(f: Path) -> bool:
    st = f.stat()
    return st.st_nlink > 1 or not st.st_mode & stat.S_IWUSR


def materialize(project: Project, paths: list[Path]) -> int:
    """Make the folders of `paths` real copies in this lane folder (each file that is still the tag's: its own copy). Returns how many."""
    root = project.root
    done = 0
    for d in dict.fromkeys(p if p.is_dir() else p.parent for p in paths):
        try:
            d.resolve().relative_to(root.resolve())
        except ValueError:
            continue
        for f in ([*d.rglob("*")] if d.is_dir() else []):
            if f.is_file() and not f.is_symlink() and _shared(f):
                tmp = f.with_name(f".{f.name}.q3tui-cow")
                shutil.copyfile(f, tmp)
                os.chmod(tmp, f.stat().st_mode | stat.S_IWUSR)
                os.replace(tmp, f)
                done += 1
    return done


def _append(project: Project, entry: dict) -> None:
    with (project.ensure_state_dir() / CHANGES).open("a") as out:
        out.write(json.dumps({**entry, "at": _now()}) + "\n")


def read_changes(folder: Path) -> list[dict]:
    f = folder / ".q3tui" / CHANGES
    if not f.is_file():
        return []
    return [json.loads(x) for x in f.read_text().splitlines() if x.strip()]


# -- engine hooks (no-ops outside a lane folder) ----------------------------------------------------------------------


def before_step(engine, step) -> None:
    """In a lane: the folders the step writes become its own copies first."""
    if lane_info(engine.project):
        n = materialize(engine.project, step.outputs(engine))
        if n:
            engine.bus.emit("log", step.name, message=f"lane: {n} file(s) of the tag copied before {step.name} writes them")


def after_step(engine, step, feedback: list[str]) -> None:
    """In a lane: an own step that applied change requests (a fix of the lane's own files) is taken as it is at the merge."""
    if feedback and lane_info(engine.project) and step.name not in design_steps(engine.project):
        _append(engine.project, {"kind": "own", "step": step.name, "requests": feedback})


def note_request(project: Project, target: str, feedback: str, origin: str | None) -> None:
    """In a lane: a change request to a design step from the lane's own work (or from you) is recorded for the merge. The design steps'
    requests to each other are not: the merge's own run makes them again."""
    if not lane_info(project) or target not in design_steps(project) or (origin and origin in design_steps(project)):
        return
    _append(project, {"kind": "request", "target": target, "feedback": feedback, "origin": origin or "you"})


# -- running the lanes ------------------------------------------------------------------------------------------------


def _metric_failed(eng) -> str:
    rec = next(((n, r) for n, r in eng.state.steps.items() if r.status == "failed" and n in eng.by_name), None)
    return f"{rec[0]}: {(rec[1].error or '').splitlines()[0][:300]}" if rec else ""


async def run_lane(project: Project, tag: str, lane: str, bus=None) -> dict:
    """One lane on a tag, in its folder: the flow runs unattended (its gates approved) with its own fix loop (the dv triage, the FPGA PPA
    hand-off; the Sanity flow: `q3tui loop`); its result is recorded with the tag."""
    from q3tui import flows
    from q3tui.core.events import EventBus
    from q3tui.pipeline.engine import Engine

    bus = bus or EventBus()
    d = make_lane(project, tag, lane)
    lp = Project.open(d)
    flow = flows.load_flow(lane, d)
    cost0 = Engine(lp, flow=flow).state.total_cost_usd
    started, detail = _now(), ""
    try:
        if any(hasattr(s, "pending_handoff") for s in flows.build_steps(flow)):  # (the Sanity flow: analysis → hand-off → owner flow)
            from q3tui.core import loop

            res = await loop.fix_loop(lp, bus, flow.name)
            outcome, detail = ("complete" if res.stopped == "clean" else "failed"), res.stopped
        else:
            outcome = await Engine(lp, bus, flow=flow).run(yes=True)
    except Exception as exc:  # noqa: BLE001 - one lane's crash is that lane's result, the others go on
        outcome, detail = "error", f"{type(exc).__name__}: {exc}"
    eng = Engine(lp, flow=flow)
    changes = read_changes(d)
    blocking = [q["id"] for q in eng.questions() if q.get("status") == "open" and q.get("blocking")]
    if outcome == "complete":
        status = "fixed" if changes else "passed"
    elif blocking:
        status = "needs_you"
    else:
        status = "failed"
    if status not in ("passed", "fixed") and not detail:
        detail = _metric_failed(eng) or outcome
    if status == "needs_you":
        detail = f"{', '.join(blocking[:8])} wait for your answer (q3tui -C {d} answer …, or open the TUI there), then `q3tui lanes run --tag {tag}`"
    result = {"lane": lane, "tag": tag, "status": status, "outcome": outcome, "detail": detail, "folder": str(d),
              "requests": sum(c["kind"] == "request" for c in changes), "own_fixes": sorted({c["step"] for c in changes if c["kind"] == "own"}),
              "cost_usd": round(eng.state.total_cost_usd - cost0, 4), "started": started, "ended": _now()}
    write_json(tags_dir(project) / tag / "results" / f"{lane}.json", result)
    return result


def lanes_of(project: Project, lanes: list[str] | None) -> list[str]:
    from q3tui import flows
    from q3tui.pipeline.engine import EngineError

    names = list(dict.fromkeys(lanes or project.cfg.lanes.flows))
    known = flows.list_flows(project.root)
    missing = [n for n in names if n not in known]
    if missing:
        raise EngineError(f"no flow {', '.join(missing)} (lanes are flows: {', '.join(sorted(known))})")
    return names


async def run_lanes(project: Project, tag: str | None = None, lanes: list[str] | None = None, printer=None) -> dict[str, dict]:
    """Every lane on the tag (default: the latest), at the same time. `printer(lane, event)` sees each lane's events."""
    import anyio

    from q3tui.core.events import EventBus
    from q3tui.pipeline.engine import EngineError

    tag = tag or latest_tag(project)
    if not tag:
        raise EngineError("no tag yet: `q3tui tag` freezes the design first")
    names = lanes_of(project, lanes)
    results: dict[str, dict] = {}

    async def one(lane: str) -> None:
        bus = EventBus()
        if printer is not None:
            bus.subscribe(lambda e, lane=lane: printer(lane, e))
        results[lane] = await run_lane(project, tag, lane, bus)

    async with anyio.create_task_group() as tg:
        for n in names:
            tg.start_soon(one, n)
    return {n: results[n] for n in names}


# -- merge ---------------------------------------------------------------------------------------------------------------


def _unit(target: str, feedback: str) -> str:
    """The unit a change request is for: `[rtl:m_x] …` → rtl:m_x, else the step."""
    text = feedback.split("] ", 1)[1] if feedback.startswith("[decision]") else feedback
    if text.startswith("[") and "]" in text:
        scope = text[1:text.index("]")]
        if ":" in scope:
            return scope
    return target


@dataclass
class MergePlan:
    tag: str
    lanes: list[str]  # the lanes whose change requests are replayed
    requests: list[dict] = field(default_factory=list)  # {lane, target, feedback, origin}
    own: dict[str, str] = field(default_factory=dict)  # own step → the lane whose files and records are taken
    overlaps: list[str] = field(default_factory=list)  # units more than one lane changed (both fixes go to the owning step)
    conflicts: list[str] = field(default_factory=list)  # own steps two lanes left different (one has to be chosen)


def _lane_engine(folder: Path):
    from q3tui import flows
    from q3tui.pipeline.engine import Engine

    info = read_json(folder / ".q3tui" / LANE_FILE, {}) or {}
    return Engine(Project.open(folder), flow=flows.load_flow(info.get("lane") or folder.name, folder))


def merge_plan(project: Project, tag: str, lanes: list[str] | None = None) -> MergePlan:
    """What a merge takes: the change requests of the lanes that fixed something (or of `lanes`), and the own steps (their files, their
    records) of those lanes and of the lanes that passed: a result the next tag does not make stale is kept, the lane does not run it again."""
    from q3tui.core.project import sha256_json
    from q3tui.pipeline.state import PipelineState

    results = tag_results(project, tag)
    chosen = list(lanes) if lanes else [n for n, r in results.items() if r.get("status") == "fixed"]
    plan = MergePlan(tag, chosen)
    units: dict[str, set[str]] = {}
    for lane in chosen:
        for c in read_changes(lane_dir(project, tag, lane)):
            if c["kind"] == "request":
                plan.requests.append({"lane": lane, "target": c["target"], "feedback": c["feedback"], "origin": c.get("origin", "")})
                units.setdefault(_unit(c["target"], c["feedback"]), set()).add(lane)
    plan.overlaps = [f"{u}: {', '.join(sorted(ls))}" for u, ls in sorted(units.items()) if len(ls) > 1]
    tag_state = PipelineState.load(tags_dir(project) / tag / "pipeline.json")
    design = design_steps(project)
    versions: dict[str, dict[str, str]] = {}
    # (lanes you name: theirs only — how a conflict is settled; else the lanes that fixed something and the ones that passed)
    sources = chosen if lanes else [*chosen, *(n for n, r in results.items() if r.get("status") == "passed" and n not in chosen)]
    for lane in sources:
        folder = lane_dir(project, tag, lane)
        if not (folder / ".q3tui" / LANE_FILE).is_file():
            continue
        leng = _lane_engine(folder)
        for s in leng.steps:
            rec = leng.state.steps.get(s.name)
            if s.name not in design and rec is not None and rec.status == "done":
                versions.setdefault(s.name, {})[lane] = sha256_json(rec.outputs)
    for step, vs in sorted(versions.items()):
        before = tag_state.steps.get(step)
        changed = {lane: h for lane, h in vs.items() if before is None or h != sha256_json(before.outputs) or before.status != "done"}
        if len(set(changed.values())) > 1:
            plan.conflicts.append(f"{step}: {', '.join(changed)}")
        elif changed:
            plan.own[step] = next(iter(changed))
    return plan


def _take_own(project: Project, main_eng, lane_folder: Path, step_name: str) -> list[str]:
    """A lane's own step as the lane left it: its files and its records."""
    leng = _lane_engine(lane_folder)
    step = leng.by_name.get(step_name)
    if step is None:
        return []
    lp = leng.project
    files = [*step.outputs(leng), *sorted((lp.schemas_dir / "questions").glob(f"{step_name}.*"))]
    rels = [lp.rel(f) for f in files if f.is_file()]
    project.backup([project.root / r for r in rels])
    for r in rels:
        dest = project.root / r
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(lane_folder / r, dest)
    st, mst = leng.state, main_eng.state
    for part in ("steps", "step_meta", "gates", "closed_questions", "accepted_defaults"):
        src = getattr(st, part)
        if step_name in src:
            getattr(mst, part)[step_name] = src[step_name]
    return rels


async def merge(project: Project, tag: str | None = None, lanes: list[str] | None = None, bus=None) -> dict:
    """The fixes of the lanes that fixed something on the tag (or of `lanes`), in the project folder: their own steps as they left them, their
    change requests to the design steps replayed, the design flows run, the next tag made."""
    from q3tui import flows
    from q3tui.core.events import EventBus
    from q3tui.pipeline.engine import Engine, EngineError

    bus = bus or EventBus()
    tag = tag or latest_tag(project)
    if not tag:
        raise EngineError("no tag yet")
    plan = merge_plan(project, tag, lanes)
    if plan.conflicts:
        raise EngineError("more than one lane changed the same step of its own: " + "; ".join(plan.conflicts)
                          + " — merge one of them (q3tui lanes merge --lanes <lane>)")
    if not plan.lanes and not plan.own:
        raise EngineError(f"nothing to merge: no lane changed anything on {tag}")
    main = Engine(project, bus)
    other = next((r for r in main.other_runs() if r.get("phase") != "merge_wait"), None)  # (a fork waiting to merge its own fixes is no run)
    if other:
        raise EngineError(f"a run is going on in the project folder (pid {other['pid']}, {other['flow']}): merge when it ends")
    taken = []
    for step_name, lane in plan.own.items():
        taken += _take_own(project, main, lane_dir(project, tag, lane), step_name)
    main.save()
    engines = {f: Engine(project, bus, flow=flows.load_flow(f, project.root)) for f in project.cfg.lanes.design_flows}
    for r in plan.requests:
        eng = next((e for e in engines.values() if r["target"] in e.by_name), None)
        if eng is None:
            continue
        eng.reload()
        eng.request_change(r["target"], r["feedback"])
    for f in project.cfg.lanes.design_flows:
        eng = Engine(project, bus, flow=flows.load_flow(f, project.root))
        outcome = await eng.run(yes=True)
        if outcome != "complete":
            return {"merged": False, "from": tag, "lanes": plan.lanes, "stopped": f"the {f} flow ended '{outcome}' in the project folder: "
                    f"{_metric_failed(eng) or 'see its log'}; the next tag waits", "overlaps": plan.overlaps}
    info = make_tag(project)
    return {"merged": True, "from": tag, "tag": info["name"], "lanes": plan.lanes, "requests": len(plan.requests), "own": sorted(plan.own),
            "files": taken, "overlaps": plan.overlaps}


# -- a checking run that forks -------------------------------------------------------------------------------------------


async def fork_run(engine, fork, yes: bool) -> str:
    """`r` on a checking flow (lanes.flows) runs it in the project folder beside the others; when its run is about to change the design (a design
    step with a change request: the dv triage's RTL bug, the PPA hand-off, a decision for the spec) it goes on here, in a copy: the design as it
    is becomes a tag, the flow gets its lane folder over it (copy on write), the change requests move there (recorded for the merge), the design
    flows apply them in the copy and the flow runs again on that design, with its own fix loop. The project folder's design never changes under
    the other runs. A fork that fixed something merges back by itself (unattended, `lanes.auto_merge`) once no other run is going on in the
    project folder; else it waits for `q3tui lanes merge` (the Lanes screen's m). Returns the run's outcome."""
    from q3tui import flows
    from q3tui.pipeline.engine import RUN_FLOW, Engine

    project, flow, bus = engine.project, engine.flow.name, engine.bus
    engine.save()
    main = Engine(project, flow=engine.flow)  # (as pipeline.json has it: the change requests the design flows' engines queued)
    ds = design_steps(project)
    moved = {s: list(fb) for s, fb in main.state.feedback.items() if s in ds and fb}
    info = make_tag(project, fork=flow)
    tag = info["name"]
    d = make_lane(project, tag, flow)
    lp = Project.open(d)
    for target, fbs in moved.items():
        for fb in fbs:
            _append(lp, {"kind": "request", "target": target, "feedback": fb, "origin": f"{flow} (forked at {fork.step})"})
    for target, fbs in moved.items():  # (the copy's now: replayed by the merge, never applied here meanwhile)
        main.state.feedback[target] = [x for x in main.state.feedback.get(target, []) if x not in fbs]
        if not main.state.feedback[target]:
            main.state.feedback.pop(target)
    for name in [s.name for s in main.steps]:  # (its fix loop goes on in the copy)
        meta = main.state.step_meta.get(name)
        if isinstance(meta, dict):
            meta.pop("loop", None)
            meta.pop("run_flows", None)
    main.save()
    bus.emit("log", None, message=f"{flow}: the fix changes the design ({', '.join(sorted(moved)) or fork.step}): going on in its own copy "
                                  f"{d} over tag {tag}; the project folder stays as it is for the other runs")
    for f in project.cfg.lanes.design_flows:  # the fix applied in the copy
        out = await Engine(lp, bus, flow=flows.load_flow(f, d)).run(yes=True)
        if out != "complete":
            bus.emit("warning", None, message=f"{flow} (copy {tag}): the {f} flow ended '{out}' in the copy ({d}): look there, then `q3tui lanes run --tag {tag} --lanes {flow}`")
            return "failed" if out == "failed" else "blocked"
    result = await run_lane(project, tag, flow, bus)  # the flow again on the fixed design: its fix loop goes on in the copy
    bus.emit("log", None, message=f"{flow} (copy {tag}): {result['status']}" + (f" — {result['detail']}" if result.get("detail") else ""))
    if result["status"] != "fixed":
        return {"passed": "complete", "needs_you": "blocked"}.get(result["status"], "failed")
    if not (project.cfg.lanes.auto_merge and (yes or engine.unattended_run())):
        bus.emit("log", None, message=f"{flow}: its fixes wait in the copy: merge them with m on the Lanes screen (q3tui lanes merge --tag {tag} --lanes {flow})")
        return "complete"
    held = await _wait_alone(engine)
    token = RUN_FLOW.set(None)  # (the merge runs the design flows in the project folder: it is the change, not a checking run)
    try:
        m = await merge(project, tag, [flow], bus)
    finally:
        RUN_FLOW.reset(token)
        held.close()  # (the next fork's merge may go)
        engine._take_lock()
    bus.emit("log", None, message=f"{flow}: fixes merged into the project folder (tag {m['tag']})" if m.get("merged")
             else f"{flow}: merge stopped: {m.get('stopped')}")
    return "complete" if m.get("merged") else "failed"


async def _wait_alone(engine):
    """Until no other run is going on in the project folder (a merge changes the design under them); forks waiting to merge wait for each
    other's merge, one at a time: returns the held `merge.lock` (closing it lets the next one go)."""
    import anyio

    engine._take_lock(phase="merge_wait")
    said = False
    while True:
        busy = [r for r in engine.other_runs() if r.get("phase") != "merge_wait"]
        if not busy:
            break
        if not said:
            engine.bus.emit("log", None, message=f"{engine.flow.name}: merging when the run(s) of {', '.join(sorted({r['flow'] for r in busy}))} end")
            said = True
        await anyio.sleep(engine.WAIT_S)
    import fcntl

    fh = open(engine.project.state_dir / "merge.lock", "a")
    await anyio.to_thread.run_sync(lambda: fcntl.flock(fh, fcntl.LOCK_EX))
    return fh


# -- converge ------------------------------------------------------------------------------------------------------------


@dataclass
class Round:
    number: int
    tag: str
    results: dict[str, str]  # lane → status
    cost_usd: float
    note: str = ""

    @property
    def failing(self) -> int:
        return sum(s not in STATUS_OK for s in self.results.values())


@dataclass
class ConvergeResult:
    stopped: str
    rounds: list[Round] = field(default_factory=list)
    best_tag: str | None = None
    converged: str | None = None

    @property
    def ok(self) -> bool:
        return self.converged is not None


async def converge(project: Project, lanes: list[str] | None = None, max_rounds: int | None = None, max_cost_usd: float | None = None,
                   merge_ok: bool = True, printer=None, bus=None) -> ConvergeResult:
    """Lanes on the tag → merge the fixes → the next tag → lanes again, until every lane passes on one tag. Stops when a round is not better
    than the best one (fewer lanes failing), at the round or spend limit, when nothing was fixed (a lane waits for you), or before a merge
    when `merge_ok` is False (the merge waits for you)."""
    names = lanes_of(project, lanes)
    limit = max_rounds or project.cfg.lanes.max_rounds or project.cfg.pipeline.max_fix_iterations
    budget = max_cost_usd or project.cfg.lanes.max_cost_usd
    tag = latest_tag(project)
    if tag is None or design_changed_since(project, tag):
        tag = make_tag(project)["name"]
    result = ConvergeResult("")
    spent = 0.0
    best: tuple[int, str] | None = None
    for number in range(1, limit + 1):
        results = await run_lanes(project, tag, names, printer)
        cost = sum(r.get("cost_usd", 0.0) for r in results.values())
        spent += cost
        rnd = Round(number, tag, {n: r["status"] for n, r in results.items()}, cost)
        result.rounds.append(rnd)
        if rnd.failing == 0:
            (tags_dir(project) / tag / "converged").write_text(_now() + "\n")
            result.stopped, result.converged, result.best_tag = "converged", tag, tag
            return result
        if best is None or rnd.failing < best[0]:
            best = (rnd.failing, tag)
        elif number > 1:
            result.stopped, result.best_tag = "no improvement", best[1]
            rnd.note = f"not better than {best[1]} ({best[0]} lane(s) failing)"
            return result
        result.best_tag = best[1]
        if budget is not None and spent >= budget:
            result.stopped = "spend limit"
            return result
        fixed = [n for n, r in results.items() if r["status"] == "fixed"]
        if not fixed:
            waiting = [n for n, r in results.items() if r["status"] == "needs_you"]
            result.stopped = f"needs you: {', '.join(waiting)} (answer in the lane folder)" if waiting else "nothing fixed"
            return result
        if not merge_ok:
            result.stopped = f"the merge of {', '.join(fixed)} waits for you: q3tui lanes merge --tag {tag}"
            return result
        m = await merge(project, tag, fixed, bus)
        if not m["merged"]:
            result.stopped = m["stopped"]
            return result
        rnd.note = f"merged {', '.join(fixed)} → {m['tag']}" + (f" (both changed: {'; '.join(m['overlaps'])})" if m["overlaps"] else "")
        tag = m["tag"]
    result.stopped = "rounds"
    return result


def clean(project: Project, tag: str | None = None) -> list[str]:
    """Delete the lane folders (of one tag, or all); the tags, their results and change requests stay."""
    root = lanes_root(project)
    gone = []
    for d in ([root / tag] if tag else sorted(p for p in root.glob("*/") if p.is_dir())) if root.is_dir() else []:
        if d.is_dir():
            for lane in sorted(d.iterdir()):
                changes = lane / ".q3tui" / CHANGES
                if changes.is_file():  # (kept with the tag: what the lane changed, and why)
                    keep = tags_dir(project) / d.name / "changes"
                    keep.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(changes, keep / f"{lane.name}.jsonl")
            shutil.rmtree(d)
            gone.append(d.name)
    return gone


# -- text -----------------------------------------------------------------------------------------------------------------


def status_text(project: Project) -> str:
    tags = list_tags(project)
    if not tags:
        return "no tag yet (q3tui tag freezes the design)"
    lines = []
    for t in tags:
        conv = (tags_dir(project) / t["name"] / "converged").is_file()
        lines.append(f"{t['name']}  {t['at'].replace('T', ' ')}  {t['files']} file(s)" + (f"  from {t['previous']}" if t.get("previous") else "")
                     + ("  CONVERGED" if conv else "") + ("  (forced: " + "; ".join(t["forced"][:3]) + ")" if t.get("forced") else ""))
        for lane, r in tag_results(project, t["name"]).items():
            extra = f" · {r['requests']} request(s) to the design" if r.get("requests") else ""
            extra += f" · own fixes: {', '.join(r['own_fixes'])}" if r.get("own_fixes") else ""
            lines.append(f"    {lane:<10} {r.get('status', '?'):<10} ${r.get('cost_usd', 0):.2f}{extra}" + (f" — {r['detail'][:160]}" if r.get("detail") else ""))
    return "\n".join(lines)


def converge_summary(res: ConvergeResult) -> str:
    lines = ["round  tag   lanes                                     cost     note"]
    for r in res.rounds:
        lanes = " ".join(f"{n}:{s}" for n, s in r.results.items())
        lines.append(f"{r.number:>5}  {r.tag:<5} {lanes:<41} ${r.cost_usd:>6.2f}  {r.note[:120]}")
    lines.append(f"stopped: {res.stopped}" + (f"; converged on {res.converged}" if res.converged else f"; best tag {res.best_tag}" if res.best_tag else ""))
    return "\n".join(lines)
