"""The plain testbench's runs: the `simulate` step's work when the testbench kind is `plain`.

The `tb` step compiled the testbench with the tool role `sim` (the simv is in build/sim/tb). Here every selected test case runs on its own with the tool role `run`
(`+TC=<tc_id>`), and the result is read from the log only: the test bench prints `TC_RESULT <tc_id> PASS|FAIL` (`TC_DETAIL <tc_id> FAIL: <msg>` for each failed
check). A test is PASS only when the simulator returned an accepted code and the log says PASS for it; no result line is a FAIL, never a guess. Every run that
happened is an entry of schemas/dv_evidence.json, `reports/dv_test_results.md` lists the tests; schemas/dv_test_results.json has every test's status while
they run (PENDING, RUNNING, then its result: the TUI's Tests tab). A failure is a finding about the DUT or the testbench, never a
reason to change the expected behavior.

A failed test whose log names an input file that exists nowhere in the project (a program list, a hex image, a golden export: `missing_inputs`) is BLOCKED, by
code: it is not triaged and nothing is handed back (the testbench cannot make those files), the step warns and lists them. Its other failures are judged once
the inputs exist."""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

from q3tui.flows.spec.lib import artifacts

RESULT = re.compile(r"^TC_RESULT\s+(\S+)\s+(PASS|FAIL)\b", re.M)
# a log line saying a file could not be read: VCS (STASKW_CO), Icarus ($readmem / $fopen), Verilator, Questa, or the testbench's own message
_NOT_OPENED = re.compile(r"could not be opened|cannot open|can't open|unable to open|failed to open|could not open|not found|no such file|missing", re.I)
_FILE = re.compile(r"[\w$./{}-]*[\w}]\.(?:list|hex|mem|layout|bin|elf|dat|txt|vmem|coe|json|csv|f)\b")
DETAIL = re.compile(r"^TC_DETAIL\s+(\S+)\s+FAIL:\s*(.*)$", re.M)


def judge(text: str, returncode: int | None, ok_codes: list[int], tc_id: str) -> tuple[str, str]:
    """(status, note) of one run of one test case, from its log."""
    if returncode not in ok_codes:
        return "FAIL", f"simulator exit code {returncode}"
    m = next((m for m in RESULT.finditer(text) if m.group(1) == tc_id), None)
    if m is None:
        return "FAIL", f"no TC_RESULT line for {tc_id} in the log"
    if m.group(2) != "PASS":
        details = [d.group(2).strip() for d in DETAIL.finditer(text) if d.group(1) == tc_id]
        return "FAIL", "; ".join(details[:3]) or "the test case reported FAIL"
    return "PASS", ""


def missing_inputs(text: str, root: Path, _seen: dict | None = None) -> list[str]:
    """The input files a test's log says it could not read and that exist nowhere under the project (by name: a testbench that probes a few relative
    paths for a file that is there is not missing anything). When the log has the testbench's own failure lines (TC_DETAIL), only those count: a file it
    probes and goes on without (an optional `.xskip`, the simulator's "Cannot open file" warning) is not why the test failed."""
    seen = {} if _seen is None else _seen  # name → found (one walk of the project per name)
    out: dict[str, str] = {}  # name → the first path the log gave for it
    lines = text.splitlines()
    details = [x for x in lines if DETAIL.search(x)]
    for line in details or lines:
        if not _NOT_OPENED.search(line):
            continue
        for path in _FILE.findall(line):
            name = Path(path).name
            if name.startswith(".") or "/" in path and path.rsplit("/", 1)[1].startswith("."):  # (".hex/.act1.hex": a list of suffixes, not a file)
                continue
            if name not in seen:
                seen[name] = Path(path).is_absolute() and Path(path).is_file() or _found(root, name)
            if not seen[name]:
                out.setdefault(name, path)
    return list(out.values())


def _found(root: Path, name: str) -> bool:
    """A file of that name somewhere in the project, its internal state (.q3tui/: backups) left out."""
    import os

    for _d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x != ".q3tui"]
        if name in files:
            return True
    return False


def _hand_blocked(step, ctx, blocked: list[tuple[str, list[str]]]) -> bool:
    """A blocked test's missing files are the `prepare` step's to make (when this flow has it and its tools are there): a change request scoped to the
    test. Files prepare listed as only yours to provide, a prepare that cannot build, or the same test sent back too often: a warning only. True: sent."""
    engine = ctx.engine
    if not blocked or "prepare" not in engine.by_name:
        return False
    from q3tui.steps.common import scoped

    man = artifacts.read(engine.project, "sw_manifest.json") or {}
    if man and not man.get("built"):
        ctx.emit("warning", message="simulate: the blocked tests wait for the prepare step's tools: " + "; ".join(man.get("problems") or []))
        return False
    external = {t["tc_id"]: set(t.get("external") or []) for t in man.get("tests") or []}
    meta = engine.state.step_meta.setdefault(step.name, {})
    hist: dict = meta.setdefault("prepare_history", {})
    sent = False
    for tc, files in blocked:
        mine = [f for f in files if Path(f).name not in {Path(e).name for e in external.get(tc, ())}]
        if not mine:
            ctx.emit("warning", message=f"simulate {tc}: waits for files only you can provide: {', '.join(files[:4])}")
            continue
        if hist.get(tc, 0) >= engine.project.cfg.pipeline.escalate_after_repeats:
            ctx.emit("warning", message=f"simulate {tc}: prepare did not make {', '.join(mine[:4])} after {hist[tc]} request(s): see the prepare step")
            continue
        hist[tc] = hist.get(tc, 0) + 1
        engine.request_change("prepare", scoped("prepare", tc, f"From the simulation ({tc}): the test could not read {', '.join(mine)}: make them, in the folder "
                                                               "and the format the testbench reads"))
        ctx.emit("log", message=f"simulate {tc}: missing input files → prepare")
        sent = True
    return sent


LIVE = "dv_test_results.json"
FINAL = ("PASS", "FAIL", "TIMEOUT", "BLOCKED", "ERROR")


class _Live:
    """schemas/dv_test_results.json: every selected test of a run, PENDING → RUNNING → its result, rewritten at each change (the TUI's Tests tab reads it
    while the tests run). `last`: the test's result in the run before; `est_s`: its time then (the last run that finished it; without this file yet, the
    evidence's); `jobs` / `order`: the slots and the order the tests start in (the TUI's time left); `pid`: the process that writes it (a run that is
    killed leaves PENDING / RUNNING tests and no `ended`: the TUI shows them as not run once that process is gone)."""

    def __init__(self, path: Path, cases: list[dict], jobs: int = 1, order: list[str] | None = None):
        import os

        try:
            old = json.loads(path.read_text()) if path.is_file() else {}
        except ValueError:
            old = {}
        before = {t.get("tc_id"): t for t in old.get("tests") or [] if isinstance(t, dict)}
        if not before:
            try:
                ev = json.loads((path.parent / "dv_evidence.json").read_text())
            except (OSError, ValueError):
                ev = {}
            before = {e.get("name"): {"est_s": e.get("duration_s")} for e in ev.get("entries") or [] if isinstance(e, dict) and e.get("kind") == "run"}

        def last(t: dict) -> str | None:
            return t.get("status") if t.get("status") in FINAL else t.get("last")

        def est(t: dict) -> float | None:
            return t["duration_s"] if t.get("status") in FINAL and t.get("duration_s") is not None else t.get("est_s")

        self.path = path
        self.data = {"schema_version": 1, "pid": os.getpid(), "started": artifacts.now(), "ended": None, "jobs": jobs,
                     "order": order or [c["tc_id"] for c in cases],
                     "tests": [{"tc_id": c["tc_id"], "name": c.get("name", ""), "req_ids": c.get("req_ids") or [], "status": "PENDING", "note": "", "log": "",
                                "t0": None, "duration_s": None, "last": last(before.get(c["tc_id"], {})), "est_s": est(before.get(c["tc_id"], {}))}
                               for c in cases]}
        self.by_id = {t["tc_id"]: t for t in self.data["tests"]}
        self.write()

    def set(self, tc: str, **kw) -> None:
        self.by_id[tc].update(kw)
        self.write()

    def close(self) -> None:
        for t in self.data["tests"]:
            if t["status"] in ("PENDING", "RUNNING"):
                t["status"], t["note"] = "NOT_RUN", "the run stopped before this test finished"
        self.data["ended"] = artifacts.now()
        self.write()

    def write(self) -> None:
        import os

        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")  # (whole or not at all: the TUI reads it while it changes)
        tmp.write_text(json.dumps(self.data, indent=2) + "\n")
        os.replace(tmp, self.path)


def bad_before(path: Path, ids: set[str]) -> dict[str, str]:
    """The tests that did not pass (FAIL / BLOCKED) in the run before, from its status file: a test that run did not finish (stopped, killed)
    counts with its result before that. They run first."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    out = {}
    for t in data.get("tests") or []:
        st = t.get("status") if t.get("status") in FINAL else t.get("last")
        if st in ("FAIL", "BLOCKED") and t.get("tc_id") in ids:
            out[t["tc_id"]] = st
    return out


def plan_cases(project) -> list[dict]:
    """The selected test cases of the testbench, with the compile status the `tb` step recorded."""
    plan = artifacts.read(project, "selected_testplan.json") or {}
    return [c for c in plan.get("test_cases", []) if c.get("selected")]


def missing_input(engine) -> str | None:
    from q3tui.eda.base import ToolUnavailable
    from q3tui.eda.tools import available, binary_of, load_tools

    p = engine.project
    plan = artifacts.read(p, "selected_testplan.json")
    if not plan:
        return "the testbench is not built yet (step tb: schemas/selected_testplan.json)"
    status = (plan.get("compile_check") or {}).get("status")
    if status == "skipped":
        return "the testbench was not compiled with the RTL (no RTL in src/rtl: run the rtl flow, then step tb again): nothing can be simulated"
    if status != "pass":
        return "the testbench does not compile with the RTL (see step tb): nothing can be simulated"
    try:
        tools = load_tools(p.cfg, p.root)
    except ToolUnavailable as exc:
        return str(exc)
    if "run" not in tools.roles:
        return "no tool for role 'run': add roles.run to config/tools.json (the command that runs the simv with +TC={tc}); `q3tui tools init` writes one to start from"
    if not available(tools, "run"):
        return f"the tool '{binary_of(tools.roles['run'])}' (role 'run') was not found (check config/tools.json, modules / setup_script)"
    return None


async def run(step, ctx) -> None:
    import anyio

    from q3tui.eda.tools import ToolBroken, broken, load_tools, run_role
    from q3tui.flows.dv.uvm import evidence as ev
    from q3tui.pipeline.base import StepFailed

    engine, project = ctx.engine, ctx.project
    lay = step.lay(engine)
    cases = plan_cases(project)
    if not cases:
        raise StepFailed("schemas/selected_testplan.json has no selected test case (step tb)")
    tools = load_tools(ctx.cfg, project.root)
    work = lay.sim / "tb"
    ok_codes = list(tools.roles["run"].ok_returncodes)
    rows, failed, blocked, timed_out, not_run, seen = [], [], [], [], [], {}
    done: dict[str, tuple] = {}  # tc → (res, status, note, missing, broken)
    limiter = anyio.CapacityLimiter(ctx.cfg.tb.sim_jobs())  # (tests at once: tb.sim_parallel, default 80 % of the cores; each one holds a simulator license)
    # the tests that failed in the run before (a fix was made for them) run first; one that fails again stops the others (killed, or not started):
    # the fix loop goes on with what failed, the tests that failed before still finish (their results go to the same triage)
    meta = engine.state.step_meta.setdefault(step.name, {})
    meta.pop("failed_before", None)  # (kept here once: the status file says it, also after a run that was killed)
    before = bad_before(lay.schemas / LIVE, {c["tc_id"] for c in cases})
    order = [c for c in cases if c["tc_id"] in before] + [c for c in cases if c["tc_id"] not in before]
    live = _Live(lay.schemas / LIVE, cases, ctx.cfg.tb.sim_jobs(), [c["tc_id"] for c in order])
    halt, abort, again = threading.Event(), threading.Event(), []  # (halt: the tests that did not fail before stop; abort: all of them)

    def why() -> str:
        return f"{again[0]} failed again after its fix (the fix loop goes on first)" if again else "the run was stopped"
    if before:
        ctx.emit("log", message=f"simulate: {', '.join(before)} did not pass in the run before: they run first")

    async def one(c: dict) -> None:
        import time

        tc = c["tc_id"]
        first = tc in before
        async with limiter:
            if halt.is_set() and not first:
                done[tc] = (None, "NOT_RUN", f"not run: {why()}", [], None)
                live.set(tc, status="NOT_RUN", note=done[tc][2])
                return
            live.set(tc, status="RUNNING", t0=time.time())
            res = await anyio.to_thread.run_sync(lambda: run_role(tools, "run", work, {"tc": tc, "top": "tb_top", "workdir": work, "project": project.root},
                                                                  log_name=f"run_{tc}.log", stop=abort if first else halt))
        missing: list[str] = []
        log = project.rel(Path(res.log_path)) if res.log_path else ""
        if res.stopped:
            done[tc] = (None, "NOT_RUN", f"stopped after {round(res.duration_s)} s: {why()}", [], None)
            live.set(tc, status="NOT_RUN", note=done[tc][2], log=log, duration_s=round(res.duration_s, 1))
            return
        if res.timed_out:  # (this test's problem with the time limit, not the tool's: the other tests still run)
            status, note = "TIMEOUT", f"killed after {round(res.duration_s)} s (the time limit of the `run` tool, `timeout_s` in config/tools.json)"
        elif bad := broken(res, "run"):
            done[tc] = (res, "", "", [], bad)
            live.set(tc, status="ERROR", note=bad, log=log, duration_s=round(res.duration_s, 1))
            return
        else:
            text = Path(res.log_path).read_text(errors="replace") if res.log_path and Path(res.log_path).is_file() else ""
            status, note = judge(text, res.returncode, ok_codes, tc)
            missing = missing_inputs(text, project.root, seen) if status == "FAIL" else []
            if missing:
                status, note = "BLOCKED", f"missing input(s): {', '.join(missing[:6])}{' …' if len(missing) > 6 else ''}" + (f" (also: {note})" if note else "")
        done[tc] = (res, status, note, missing, None)
        live.set(tc, status=status, note=note, log=log, duration_s=round(res.duration_s, 1))
        ctx.emit("log", message=f"simulation {tc} {c.get('name', '')}: {status}" + (f" ({note})" if note else "") + f" [{len(done)}/{len(cases)}]")
        if status == "FAIL" and before.get(tc) == "FAIL" and not halt.is_set():
            again.append(tc)
            halt.set()
            rest = len(cases) - len(done) - sum(t not in done for t in before)
            if rest:
                ctx.emit("warning", message=f"simulate: {tc} failed again after its fix: the other {rest} test(s) stop"
                                            + (", the ones that failed before finish" if any(t not in done for t in before) else "") + "; the fix loop goes on")

    async def watch() -> None:  # (cancelled — Ctrl-C, x in the TUI —: the running simulators are killed, not waited for)
        try:
            await anyio.sleep_forever()
        finally:
            halt.set()
            abort.set()

    try:
        async with anyio.create_task_group() as outer:
            outer.start_soon(watch)
            async with anyio.create_task_group() as tg:
                for c in order:
                    tg.start_soon(one, c)
            outer.cancel_scope.cancel()
    finally:
        live.close()
    stale = lay.schemas / "dv_evidence.json"  # (a run of this step replaces the record of the last one: kept until now, the TUI shows it meanwhile)
    if stale.is_file():
        stale.unlink()
    for c in cases:  # (in the plan's order, whichever finished first)
        tc = c["tc_id"]
        res, status, note, missing, bad = done[tc]
        if bad:
            raise ToolBroken(bad)
        if res is not None:  # (a run that was stopped, or never started, is no evidence)
            ev.record(project, "run", tc, res, note or f"{c.get('name', '')}", artifacts.now(), status=status)
        rows.append((tc, c.get("name", ""), ", ".join(c.get("req_ids") or []), status, note))
        if status == "NOT_RUN":
            not_run.append(tc)
        elif status == "TIMEOUT":
            timed_out.append(tc)
        elif status == "BLOCKED":
            blocked.append((tc, missing))
        elif status != "PASS":
            failed.append((tc, note))
    lay.reports.mkdir(parents=True, exist_ok=True)
    (lay.reports / "dv_test_results.md").write_text(
        "# Test results (released RTL as the DUT)\n\nStatus is read from the simulator log of each run (`schemas/dv_evidence.json`).\n\n| Test | Name | Requirements | Status | Note |\n|---|---|---|---|---|\n"
        + "".join(f"| {a} | `{b}` | {c} | {d} | {e} |\n" for a, b, c, d, e in rows)
        + ("\nTIMEOUT: the run was stopped at the time limit of the `run` tool (`timeout_s` in config/tools.json); it is not judged.\n" if timed_out else "")
        + ("\nBLOCKED: the test could not run for want of input files that are not in the project (programs, images, golden exports); it is neither passed "
           "nor handed back to the testbench.\n" if blocked else "")
        + (f"\nNOT_RUN: stopped, or not started, because {', '.join(again)} failed again after its fix; they run in the next round.\n" if not_run else ""))
    if blocked:
        files = sorted({f for _, m in blocked for f in m})
        ctx.emit("warning", message=f"simulate: {len(blocked)} test(s) blocked, their input files are not in the project "
                                    f"({', '.join(t for t, _ in blocked)}): {', '.join(files[:8])}{' …' if len(files) > 8 else ''}")
    if timed_out:
        ctx.emit("warning", message=f"simulate: {len(timed_out)} test(s) ran past the time limit of the `run` tool and were stopped "
                                    f"({', '.join(timed_out)}): raise `timeout_s` of the role `run` in config/tools.json; they are not judged")
    to_prepare = _hand_blocked(step, ctx, blocked)
    if failed:
        from q3tui.flows.dv import triage

        original = ("test cases that did not pass (details: reports/dv_test_results.md): " + ", ".join(f"{t} ({n})" for t, n in failed)
                    + ". The expected behavior comes from the approved specification; a failure is a finding about the DUT or the testbench, never a reason to change it")
        by_id = {c["tc_id"]: c for c in cases}
        failures = [{"id": t, "name": by_id[t].get("name", ""), "note": n, "log": project.rel(work / f"run_{t}.log"), "reqs": by_id[t].get("req_ids") or []} for t, n in failed]
        await triage.handle_failures(step, ctx, failures, lay.rtl, "plain", original)  # whose fault: the testbench goes back to tb, the RTL to the rtl flow, the spec to you
        if to_prepare:
            engine.state.step_meta[step.name]["loop"] = True
        return
    from q3tui.flows.dv import triage

    triage.reset(engine, step)
    meta = engine.state.step_meta.setdefault(step.name, {})
    if to_prepare:
        meta["loop"] = True  # the engine updates prepare on the change requests, then runs simulate again
    elif not blocked:
        meta.pop("prepare_history", None)
    ctx.emit("log", message=f"simulate: {len(rows) - len(blocked) - len(timed_out)} test case(s) PASS" + (f", {len(blocked)} blocked" if blocked else "")
             + (f", {len(timed_out)} timed out" if timed_out else ""))
