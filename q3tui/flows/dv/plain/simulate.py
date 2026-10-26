"""The plain testbench's runs: the `simulate` step's work when the testbench kind is `plain`.

The `tb` step compiled the testbench with the tool role `sim` (the simv is in build/sim/tb). Here every selected test case runs on its own with the tool role `run`
(`+TC=<tc_id>`), and the result is read from the log only: the test bench prints `TC_RESULT <tc_id> PASS|FAIL` (`TC_DETAIL <tc_id> FAIL: <msg>` for each failed
check). A test is PASS only when the simulator returned an accepted code and the log says PASS for it; no result line is a FAIL, never a guess. Every run that
happened is an entry of schemas/dv_evidence.json, `reports/dv_test_results.md` lists the tests. A failure is a finding about the DUT or the testbench, never a
reason to change the expected behavior."""

from __future__ import annotations

import re
from pathlib import Path

from q3tui.flows.spec.lib import artifacts

RESULT = re.compile(r"^TC_RESULT\s+(\S+)\s+(PASS|FAIL)\b", re.M)
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

    from q3tui.eda.tools import check_broken, load_tools, run_role
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
    for stale in (lay.schemas / "dv_evidence.json",):  # (a run of this step replaces the record of the last one)
        if stale.is_file():
            stale.unlink()
    rows, failed = [], []
    for c in cases:
        tc = c["tc_id"]
        res = await anyio.to_thread.run_sync(lambda: run_role(tools, "run", work, {"tc": tc, "top": "tb_top", "workdir": work, "project": project.root},
                                                              log_name=f"run_{tc}.log"))
        check_broken(res, "run")
        text = Path(res.log_path).read_text(errors="replace") if res.log_path and Path(res.log_path).is_file() else ""
        status, note = judge(text, res.returncode, ok_codes, tc)
        ev.record(project, "run", tc, res, note or f"{c.get('name', '')}", artifacts.now(), status=status)
        ctx.emit("log", message=f"simulation {tc} {c.get('name', '')}: {status}" + (f" ({note})" if note else ""))
        rows.append((tc, c.get("name", ""), ", ".join(c.get("req_ids") or []), status, note))
        if status != "PASS":
            failed.append((tc, note))
    lay.reports.mkdir(parents=True, exist_ok=True)
    (lay.reports / "dv_test_results.md").write_text(
        "# Test results (released RTL as the DUT)\n\nStatus is read from the simulator log of each run (`schemas/dv_evidence.json`).\n\n| Test | Name | Requirements | Status | Note |\n|---|---|---|---|---|\n"
        + "".join(f"| {a} | `{b}` | {c} | {d} | {e} |\n" for a, b, c, d, e in rows))
    if failed:
        raise StepFailed("test cases that did not pass (details: reports/dv_test_results.md): " + ", ".join(f"{t} ({n})" for t, n in failed)
                         + ". The expected behavior comes from the approved specification; a failure is a finding about the DUT or the testbench, never a reason to change it")
    ctx.emit("log", message=f"simulate: {len(rows)} test case(s) PASS")
