"""FPGA step `fpga_synth`: out-of-context synthesis of the RTL with the SDC (tool role `fpga_synth`: Vivado by default).

The Tcl script is written by code (`lib/vivado.py`); the reports go to `fpga/reports/`, a summary of the run to
`schemas/fpga_synth.json`. A tool that is not there is a failed step, never a pass."""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import anyio

from q3tui.eda import tools as toolmod
from q3tui.eda.base import ToolUnavailable
from q3tui.pipeline.base import StepContext, StepFailed
from q3tui.flows.fpga.lib import vivado
from q3tui.flows.fpga.lib.base import SYNTH_JSON, FpgaStep

REPORTS = ("utilization.rpt", "timing.rpt", "check_timing.rpt", "paths.rpt", "clocks.rpt")


def _version(log: str | None) -> str:
    import re

    m = re.search(r"Vivado v\.?(\S+)", Path(log).read_text(errors="replace")) if log and Path(log).is_file() else None
    return f"Vivado {m.group(1)}" if m else "Vivado"


class Step(FpgaStep):
    name = "fpga_synth"
    title = "FPGA synthesis"
    deps = ("sdc",)
    tool_roles = ("fpga_synth",)

    def inputs(self, engine) -> list[Path]:
        return [self.filelist(engine), *self.rtl_files(engine), *self.sdc_inputs(engine)]

    def outputs(self, engine) -> list[Path]:
        rep = self.reports_dir(engine)
        files = [rep / n for n in REPORTS if (rep / n).is_file()]
        script = engine.project.root / "fpga" / "synth.tcl"
        return [*([script] if script.is_file() else []), *files, *([self.artifact(engine, SYNTH_JSON)] if self.artifact(engine, SYNTH_JSON).is_file() else [])]

    def reset_files(self, engine) -> list[Path]:
        return self.outputs(engine)

    def missing_input(self, engine) -> str | None:
        return self.missing_rtl(engine)

    async def run(self, ctx: StepContext) -> None:
        engine, project = ctx.engine, ctx.project
        top = self.top(engine)
        try:
            tools = self.tools(engine)
            if not await anyio.to_thread.run_sync(lambda: toolmod.available(tools, "fpga_synth")):  # (`module load` runs a shell)
                raise ToolUnavailable("the `fpga_synth` tool was not found (module xilinx/vivado; see tools.json / `q3tui tools check`)")
        except ToolUnavailable as exc:
            raise StepFailed(f"FPGA synthesis not run: {exc}") from exc
        work = self.workdir(engine)
        for n in (*REPORTS, "vivado.log"):
            (work / n).unlink(missing_ok=True)
        script = project.root / "fpga" / "synth.tcl"
        script.parent.mkdir(parents=True, exist_ok=True)
        project.backup([script])
        script.write_text(vivado.script_text(self.rtl_files(engine), top, project.root / "sdc", work, self.options))
        part = (self.options.get("fpga") or {}).get("part") or vivado.DEFAULT_PART
        ctx.emit("log", message=f"synthesizing {top} for {part} (out of context)…")
        t0 = time.monotonic()
        # in a thread: the tool runs for minutes, and the TUI (same event loop) must stay alive meanwhile
        done = anyio.Event()

        async def watch() -> None:
            """The tool writes {workdir}/vivado.log as it goes: its phases are the progress; a quiet minute gets a heartbeat."""
            tail, quiet = vivado.LogTail(work / "vivado.log"), 0.0
            while not done.is_set():
                with anyio.move_on_after(2):
                    await done.wait()
                shown = [p for p in map(vivado.phase_line, tail.new()) if p]
                for p in shown:
                    ctx.emit("log", message=f"vivado: {p}")
                quiet = 0.0 if shown else quiet + 2
                if quiet >= 30:
                    ctx.emit("log", message=f"vivado: still running ({time.monotonic() - t0:.0f} s)…")
                    quiet = 0.0

        finished = False
        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(watch)
                try:
                    res = await anyio.to_thread.run_sync(lambda: toolmod.run_role(
                        tools, "fpga_synth", work, {"script": script, "top": top, "filelist": self.filelist(engine),
                                                    "files": self.rtl_files(engine), "workdir": work, "part": part}), abandon_on_cancel=True)
                    finished = True
                finally:
                    done.set()
        finally:
            if not finished:  # stopped (x) or failed: the thread is let go, so the tool must not be left running
                with anyio.CancelScope(shield=True):  # (a cancelled scope would skip the await)
                    await anyio.to_thread.run_sync(lambda: subprocess.run(["pkill", "-TERM", "-f", str(script)], capture_output=True))
        toolmod.check_broken(res, "fpga_synth")
        if not res.ok:
            errs = "; ".join(d.message for d in res.errors[:3])
            raise StepFailed(f"synthesis failed: {errs} (log: {res.log_path})")
        rep = self.reports_dir(engine)
        rep.mkdir(parents=True, exist_ok=True)
        got = []
        for n in REPORTS:
            if (work / n).is_file():
                shutil.copy2(work / n, rep / n)
                got.append(n)
        if "utilization.rpt" not in got or "timing.rpt" not in got:
            raise StepFailed(f"the tool finished but wrote no utilization / timing report (log: {res.log_path})")
        critical = [d.message for d in res.diagnostics if d.code == "CRITICAL WARNING"]
        crit = len(critical)
        for m in critical[:3]:  # (an SDC that matched nothing shows up here first)
            ctx.emit("warning", message=f"vivado critical warning: {m[:200]}")
        self.write(engine, SYNTH_JSON, {"tool": _version(res.log_path), "part": part, "top": top, "mode": "out_of_context",
                                        "implemented": bool((self.options.get("fpga") or {}).get("implement")), "status": "pass",
                                        "runtime_s": round(time.monotonic() - t0, 1), "log": project.rel(Path(res.log_path)),
                                        "warnings": sum(d.severity == "warning" for d in res.diagnostics) - crit,
                                        "critical_warnings": crit, "critical_warning_messages": list(dict.fromkeys(critical))[:10], "reports": got})
        ctx.emit("log", message=f"synthesis done in {time.monotonic() - t0:.0f} s ({crit} critical warning(s)); reports in fpga/reports/")
