"""FPGA step `fpga_synth`: out-of-context synthesis of the RTL with the SDC, for the template chosen in `options.template`
(`lib/templates.py`): Xilinx 7-series with Vivado (tool role `fpga_synth`, the default), or Lattice ECP5 / iCE40 with Yosys and
nextpnr (roles `fpga_yosys`, `fpga_pnr_ecp5` / `fpga_pnr_ice40`: no licence).

The scripts are written by code (`lib/vivado.py`, `lib/oss.py`); the reports go to `build/syn/fpga/reports/` with the same names for every
template (utilization / timing / paths), a summary of the run to `schemas/fpga_synth.json`. A tool that is not there is a failed
step, never a pass."""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import anyio

from q3tui.eda import tools as toolmod
from q3tui.eda.base import ToolUnavailable
from q3tui.pipeline.base import StepContext, StepFailed
from q3tui.flows.fpga.lib import oss, vivado
from q3tui.flows.fpga.lib import sdc as sdcmod
from q3tui.flows.fpga.lib.base import SYNTH_JSON, FpgaStep

REPORTS = ("utilization.rpt", "timing.rpt", "check_timing.rpt", "paths.rpt", "clocks.rpt", "pnr.rpt", "yosys_stat.rpt")


def _version(log: str | None) -> str:
    import re

    m = re.search(r"Vivado v\.?(\S+)", Path(log).read_text(errors="replace")) if log and Path(log).is_file() else None
    return f"Vivado {m.group(1)}" if m else "Vivado"


class Step(FpgaStep):
    name = "fpga_synth"
    title = "FPGA synthesis"
    deps = ("sdc",)
    tool_roles = ("fpga_synth", "fpga_yosys", "fpga_pnr_ecp5", "fpga_pnr_ice40")
    hashed_options = ("template", "part", "package", "speed", "fpga", "frontend", "defines", "synth_args", "implement")  # (staleness: these change the result)

    def inputs(self, engine) -> list[Path]:
        return [self.filelist(engine), *self.rtl_files(engine), *self.sdc_inputs(engine)]

    def outputs(self, engine) -> list[Path]:
        rep = self.reports_dir(engine)
        files = [rep / n for n in REPORTS if (rep / n).is_file()]
        script = engine.project.fpga_dir / "synth.tcl"
        return [*([script] if script.is_file() else []), *files, *([self.artifact(engine, SYNTH_JSON)] if self.artifact(engine, SYNTH_JSON).is_file() else [])]

    def reset_files(self, engine) -> list[Path]:
        return self.outputs(engine)

    def missing_input(self, engine) -> str | None:
        return self.missing_rtl(engine)

    async def _need(self, engine, role: str, hint: str):
        """The tool set when `role` is there, else a failed step: a missing tool is never a pass."""
        try:
            tools = self.tools(engine)
            if not await anyio.to_thread.run_sync(lambda: toolmod.available(tools, role)):  # (`module load` runs a shell)
                raise ToolUnavailable(f"the `{role}` tool was not found ({hint}; see tools.json / `q3tui tools check`)")
        except ToolUnavailable as exc:
            raise StepFailed(f"FPGA synthesis not run: {exc}") from exc
        return tools

    async def _watched(self, ctx: StepContext, tools, role: str, variables: dict, work: Path, log_name: str, phase, tag: str, kill: str, tail_name: str | None = None):
        """Run a tool role in a thread (it runs for minutes and the TUI shares this event loop), showing the phases of its log as progress
        and a heartbeat when it is quiet; stopped (x) or failed, the tool is not left running."""
        t0 = time.monotonic()
        done = anyio.Event()
        tail = vivado.LogTail(work / (tail_name or log_name))  # (the tool's own log, when it writes one: it grows while the tool runs)

        async def watch() -> None:
            quiet, last = 0.0, ""
            while not done.is_set():
                with anyio.move_on_after(2):
                    await done.wait()
                shown = [p for p in map(phase, tail.new()) if p]
                for p in dict.fromkeys(shown[-3:]):
                    if p != last:  # (a pass that repeats is shown once)
                        ctx.emit("log", message=f"{tag}: {p}")
                    last = p
                quiet = 0.0 if shown else quiet + 2
                if quiet >= 30:
                    ctx.emit("log", message=f"{tag}: still running ({time.monotonic() - t0:.0f} s)…")
                    quiet = 0.0

        finished = False
        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(watch)
                try:
                    res = await anyio.to_thread.run_sync(lambda: toolmod.run_role(tools, role, work, variables, log_name=log_name), abandon_on_cancel=True)
                    finished = True
                finally:
                    done.set()
        finally:
            if not finished:  # (a cancelled scope would skip the await)
                with anyio.CancelScope(shield=True):
                    await anyio.to_thread.run_sync(lambda: subprocess.run(["pkill", "-TERM", "-f", kill], capture_output=True))
        return res

    def _fpga_cfg(self) -> dict:
        """Vivado's options: the `fpga` dict, with the flat `implement` / `synth_args` (the Settings dialog, like the open-source flow's) on top."""
        flat = {k: self.options[k] for k in ("implement", "synth_args") if self.options.get(k) not in (None, [], "")}
        return {**(self.options.get("fpga") or {}), **flat}

    async def run(self, ctx: StepContext) -> None:
        tpl = self.template()
        if tpl.backend == "yosys":
            await self._run_oss(ctx, tpl)
        else:
            await self._run_vivado(ctx, tpl)

    async def _run_vivado(self, ctx: StepContext, tpl) -> None:
        engine, project = ctx.engine, ctx.project
        top = self.top(engine)
        tools = await self._need(engine, "fpga_synth", "module xilinx/vivado")
        work = self.workdir(engine)
        for n in (*REPORTS, "vivado.log"):
            (work / n).unlink(missing_ok=True)
        part, note = self.part()
        if note:
            ctx.emit("warning", message=note)
        script = project.fpga_dir / "synth.tcl"
        script.parent.mkdir(parents=True, exist_ok=True)
        project.backup([script])
        script.write_text(vivado.script_text(self.rtl_files(engine), top, project.sdc_dir, work, {**self.options, "fpga": {**self._fpga_cfg(), "part": part}}))
        ctx.emit("log", message=f"synthesizing {top} for {part} (out of context)…")
        t0 = time.monotonic()
        res = await self._watched(ctx, tools, "fpga_synth", {"script": script, "top": top, "filelist": self.filelist(engine), "files": self.rtl_files(engine),
                                                           "workdir": work, "part": part}, work, "vivado.log", vivado.phase_line, "vivado", str(script))
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
        self.write(engine, SYNTH_JSON, {"tool": _version(res.log_path), "template": tpl.id, "backend": "vivado", "part": part, "top": top, "mode": "out_of_context",
                                        "implemented": bool(self._fpga_cfg().get("implement")), "status": "pass",
                                        "runtime_s": round(time.monotonic() - t0, 1), "log": project.rel(Path(res.log_path)),
                                        "warnings": sum(d.severity == "warning" for d in res.diagnostics) - crit,
                                        "critical_warnings": crit, "critical_warning_messages": list(dict.fromkeys(critical))[:10], "reports": got})
        ctx.emit("log", message=f"synthesis done in {time.monotonic() - t0:.0f} s ({crit} critical warning(s)); reports in build/syn/fpga/reports/")

    async def _run_oss(self, ctx: StepContext, tpl) -> None:
        """Yosys (synthesis to the family's cells) then nextpnr (place and route, out of context): the same reports as Vivado's."""
        engine, project = ctx.engine, ctx.project
        top = self.top(engine)
        tools = await self._need(engine, "fpga_yosys", "module oss-cad-suite")
        await self._need(engine, tpl.pnr_role, "module oss-cad-suite")
        work = self.workdir(engine)
        for n in (*REPORTS, "yosys.log", "yosys.out", "pnr.log", "pnr.out", "out.json", "stat.rpt", "clocks.lpf"):
            (work / n).unlink(missing_ok=True)
        device, note = self.part()
        if note:
            ctx.emit("warning", message=note)
        package = str(self.options.get("package") or tpl.devices.get(device, ""))
        speed = str(self.options.get("speed") or tpl.speed)
        sdc = sdcmod.load(project)
        clocks = [{"name": c.name, "port": c.port, "period_ns": c.period_ns} for c in (sdc.clocks if sdc else [])]
        if not clocks:
            raise StepFailed("no clock in the SDC: nextpnr needs a target frequency (run the sdc step)")
        script = project.fpga_dir / "synth.ys"
        script.parent.mkdir(parents=True, exist_ok=True)
        project.backup([script])
        js, stat = work / "out.json", work / "stat.rpt"
        script.write_text(oss.yosys_script(self.rtl_files(engine), top, tpl, js, stat, self.options))
        label = f"{tpl.family} {device}" + (f" {package}" if package else "") + (f" -{speed}" if speed else "")
        ctx.emit("log", message=f"synthesizing {top} for {label} with Yosys, then place and route with nextpnr (out of context)…")
        t0 = time.monotonic()
        res = await self._watched(ctx, tools, "fpga_yosys", {"script": script, "top": top, "files": self.rtl_files(engine), "workdir": work}, work, "yosys.out",
                                  oss.phase_line, "yosys", str(script), tail_name="yosys.log")
        toolmod.check_broken(res, "fpga_yosys")
        if not res.ok or not js.is_file():
            errs = "; ".join(d.message for d in res.errors[:3]) or "no netlist was written"
            raise StepFailed(f"synthesis failed: {errs} (log: {res.log_path})")
        lpf = work / "clocks.lpf"
        lpf.write_text(oss.lpf_text(clocks))
        args = oss.pnr_args(tpl, device, package, speed, js, work / "pnr.log", clocks, lpf)
        pnr = await self._watched(ctx, tools, tpl.pnr_role, {"args": args, "workdir": work}, work, "pnr.out", oss.phase_line, "nextpnr", str(js), tail_name="pnr.log")
        toolmod.check_broken(pnr, tpl.pnr_role)
        if not pnr.ok:
            errs = "; ".join(d.message for d in pnr.errors[:3])
            raise StepFailed(f"place and route failed: {errs} (log: {pnr.log_path}) — a design that does not fit the device, or needs more IOs than the package has, stops here: "
                             f"pick a larger device (Settings → fpga_synth → part) or package")
        log = (work / "pnr.log").read_text(errors="replace")
        stat_text = stat.read_text(errors="replace") if stat.is_file() else ""
        rep = self.reports_dir(engine)
        rep.mkdir(parents=True, exist_ok=True)
        util_block = "\n".join(ln for ln in log.splitlines() if ln.startswith(("Info: \t", "Info: Device utilisation")) or "utilisation" in ln)
        files = {"utilization.rpt": util_block + "\n\n" + stat_text, "timing.rpt": oss.timing_report(log), "paths.rpt": oss.critical_path_text(log),
                 "pnr.rpt": log, "yosys_stat.rpt": stat_text}
        for n, text in files.items():
            (rep / n).write_text(text)
        if not oss.fmax_lines(log):
            raise StepFailed(f"nextpnr reported no clock frequency (log: {pnr.log_path}): no clock net of the design matched a clock of the SDC")
        warnings = sum(d.severity == "warning" for d in [*res.diagnostics, *pnr.diagnostics])
        self.write(engine, SYNTH_JSON, {"tool": oss.tool_version(Path(res.log_path).read_text(errors="replace") if Path(res.log_path).is_file() else ""),
                                        "template": tpl.id, "backend": "yosys", "family": tpl.family, "part": label, "device": device, "package": package, "speed": speed,
                                        "top": top, "mode": "out_of_context", "implemented": True, "status": "pass", "runtime_s": round(time.monotonic() - t0, 1),
                                        "log": project.rel(work / "pnr.log"), "warnings": warnings, "critical_warnings": 0, "critical_warning_messages": [], "reports": list(files)})
        ctx.emit("log", message=f"synthesis, place and route done in {time.monotonic() - t0:.0f} s; reports in build/syn/fpga/reports/")
