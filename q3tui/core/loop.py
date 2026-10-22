"""The fix loop (`q3tui loop`): analysis flow → hand-off → owner flow → analysis again, bounded, with a regression check.

The Sanity flow's waiver review proposes RTL / spec fixes; `sanity_handoff` sends them to the owner flow (rtl) as scoped change requests; the
owner flow updates the RTL; the analysis runs again. Each round is measured by the violations still open after the waivers. The loop stops when
nothing is open, when a round does not improve on the best one (the RTL of the best round is kept in `.q3tui/loop/best_rtl/`: `restore_best`),
when nothing can be handed off, when a flow ends in a failure, or when the spend limit is reached.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from q3tui import flows
from q3tui.core.project import Project, read_json, write_json
from q3tui.pipeline.engine import Engine, EngineError


@dataclass
class Round:
    number: int
    open: int  # violations still open after the waivers, all lanes
    total: int  # violations found, all lanes
    cost_usd: float  # spent in this round
    note: str = ""


@dataclass
class LoopResult:
    stopped: str  # why (clean | no improvement | nothing to hand off | owner flow ended … | analysis flow ended … | spend limit | rounds)
    rounds: list[Round] = field(default_factory=list)
    best: int | None = None  # the round with the fewest open violations
    best_dir: Path | None = None

    @property
    def ok(self) -> bool:
        return self.stopped in ("clean", "rounds", "no improvement", "nothing to hand off")


def measure(project: Project) -> tuple[int, int]:
    """(open after the waivers, found) over the lanes that have a check result (`schemas/<lane>_check.json`)."""
    open_n = total = 0
    for f in sorted(project.schemas_dir.glob("*_check.json")):
        data = read_json(f, {}) or {}
        open_n += int(data.get("open", 0) or 0)
        total += int(data.get("total", 0) or 0)
    return open_n, total


def best_dir(project: Project) -> Path:
    return project.state_dir / "loop" / "best_rtl"


def _keep_best(project: Project, rnd: Round) -> Path:
    dest = best_dir(project)
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    rtl = project.rtl_dir
    for f in sorted(rtl.rglob("*")):
        if f.is_file() and "smoke" not in f.relative_to(rtl).parts and "style_lint" not in f.relative_to(rtl).parts:
            (dest / f.relative_to(rtl)).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest / f.relative_to(rtl))
    write_json(dest.parent / "best.json", {"round": rnd.number, "open": rnd.open, "total": rnd.total, "at": datetime.now().isoformat(timespec="seconds")})
    return dest


def restore_best(project: Project) -> str:
    """Put the RTL of the best round back (what it replaces is backed up). The restored files count as your own edits afterwards."""
    src = best_dir(project)
    info = read_json(src.parent / "best.json", None)
    if not src.is_dir() or not info:
        raise EngineError("no best round recorded: run `q3tui loop` first")
    files = [f for f in sorted(src.rglob("*")) if f.is_file()]
    project.backup([project.rtl_dir / f.relative_to(src) for f in files])
    for f in files:
        dest = project.rtl_dir / f.relative_to(src)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dest)
    return f"restored the RTL of round {info['round']} ({info['open']} open of {info['total']}): {len(files)} file(s) in {project.rel(project.rtl_dir)}"


async def fix_loop(project: Project, bus, analysis_flow: str, owner: str | None = None, max_rounds: int | None = None,
                   max_cost_usd: float | None = None) -> LoopResult:
    from q3tui.core.ops import Ops

    flow = flows.load_flow(analysis_flow, project.root)
    limit = max_rounds or project.cfg.pipeline.max_fix_iterations

    def engine(f=flow) -> Engine:
        return Engine(project, bus, flow=f)

    def spent() -> float:
        return Engine(project, flow=flow).state.total_cost_usd

    start_cost = last_cost = spent()
    result = LoopResult("")
    outcome = await engine().run(yes=True)
    if outcome not in ("complete", "gate"):
        result.stopped = f"analysis flow ended '{outcome}'"
        return result
    n_open, n_total = measure(project)
    rnd = Round(0, n_open, n_total, spent() - last_cost, "first analysis")
    result.rounds.append(rnd)
    result.best, result.best_dir = 0, _keep_best(project, rnd)
    last_cost = spent()
    best_open = n_open
    for number in range(1, limit + 1):
        if best_open == 0:
            result.stopped = "clean"
            return result
        if max_cost_usd is not None and spent() - start_cost >= max_cost_usd:
            result.stopped = "spend limit"
            return result
        eng = engine()
        try:
            msg = Ops(eng).run_action("sanity_handoff")
        except EngineError as exc:
            result.stopped = "nothing to hand off"
            result.rounds[-1].note += f" · {exc}"
            return result
        owner_name = owner or next((str(s.options.get("handoff_flow") or "rtl") for s in eng.steps if hasattr(s, "pending_handoff")), "rtl")
        outcome = await engine(flows.load_flow(owner_name, project.root)).run(yes=True)
        if outcome not in ("complete", "gate"):
            result.stopped = f"owner flow '{owner_name}' ended '{outcome}'"
            result.rounds.append(Round(number, n_open, n_total, spent() - last_cost, f"{msg}; the owner flow did not finish"))
            return result
        outcome = await engine().run(yes=True)
        if outcome not in ("complete", "gate"):
            result.stopped = f"analysis flow ended '{outcome}'"
            return result
        n_open, n_total = measure(project)
        rnd = Round(number, n_open, n_total, spent() - last_cost, msg)
        result.rounds.append(rnd)
        last_cost = spent()
        if n_open < best_open:
            best_open, result.best, result.best_dir = n_open, number, _keep_best(project, rnd)
        else:
            result.stopped = "no improvement"
            rnd.note += f" · not better than round {result.best} ({best_open} open): stopped"
            return result
    result.stopped = "clean" if best_open == 0 else "rounds"
    return result


def summary(result: LoopResult) -> str:
    lines = ["round  open  found  cost     what"]
    for r in result.rounds:
        lines.append(f"{r.number:>5}  {r.open:>4}  {r.total:>5}  ${r.cost_usd:>6.2f}  {r.note[:150]}")
    lines.append(f"stopped: {result.stopped}" + (f"; best round {result.best} ({next(r.open for r in result.rounds if r.number == result.best)} open), "
                                                  f"RTL kept in {result.best_dir}" if result.best is not None else ""))
    return "\n".join(lines)
