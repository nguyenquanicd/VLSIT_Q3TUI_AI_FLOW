"""FPGA step `fpga_synth`: synthesis of the RTL with the SDC for the part in `options.part` (the project flow's FPGA view); the part's family
decides the tool (`lib/templates.py`): Xilinx 7-series with Vivado (tool role `fpga_synth`, out of context), Lattice ECP5 / iCE40 with Yosys and
nextpnr (roles `fpga_yosys`, `fpga_pnr_ecp5` / `fpga_pnr_ice40`: no licence), Intel with Quartus Prime (roles `fpga_quartus_pro` /
`fpga_quartus_std`: synthesis, the fit with `implement`, the Timing Analyzer).

The scripts are written by code (`lib/vivado.py`, `lib/oss.py`); the reports go to `build/syn/fpga/reports/` with the same names for every
template (utilization / timing / paths), a summary of the run to `schemas/fpga_synth.json`. A tool that is not there is a failed
step, never a pass.

Every other problem goes back to `fpga_port` (the step that makes the RTL ready for the part): a synthesis the tool failed (its error
lines), a memory initialization file the tool would read and is not there (said before the tool runs), a part whose family the port has
not ported for. The step hands it over as a change request and the fix loop runs `fpga_port`, then the synthesis again, at most
`max_rounds` times in a run (`pipeline.max_fix_iterations`); `schemas/fpga_synth.json` says `failed` meanwhile."""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path

import anyio

from q3tui.eda import tools as toolmod
from q3tui.eda.base import ToolUnavailable
from q3tui.pipeline.base import StepContext, StepFailed
from q3tui.flows.fpga.lib import oss, quartus, vivado
from q3tui.flows.fpga.lib import port as portmod
from q3tui.flows.fpga.lib import sdc as sdcmod
from q3tui.flows.fpga.lib.base import SYNTH_JSON, FpgaStep

REPORTS = ("utilization.rpt", "utilization_hier.rpt", "timing.rpt", "check_timing.rpt", "paths.rpt", "clocks.rpt", "pnr.rpt", "yosys_stat.rpt")


class SynthFailed(StepFailed):
    """A failure fpga_port can act on: `evidence` is what it is handed (the tool's error lines)."""

    def __init__(self, message: str, evidence: str = ""):
        super().__init__(message)
        self.evidence = evidence


_ERROR_LINE = re.compile(r"^\s*(?:ERROR|Error|CRITICAL WARNING|Critical Warning)\b")


def _error_lines(log: str, limit: int = 40) -> list[str]:
    """The error lines of a tool's log (Vivado, Quartus, Yosys, nextpnr), each with the line after it."""
    lines, out = log.splitlines(), []
    for i, ln in enumerate(lines):
        if _ERROR_LINE.match(ln):
            out += [x[:400] for x in lines[i:i + 2] if x[:400] not in out]
        if len(out) >= limit:
            break
    return out


def _version(log: str | None) -> str:
    m = re.search(r"Vivado v\.?(\S+)", Path(log).read_text(errors="replace")) if log and Path(log).is_file() else None
    return f"Vivado {m.group(1)}" if m else "Vivado"


class Step(FpgaStep):
    name = "fpga_synth"
    title = "FPGA synthesis"
    deps = ("sdc", "fpga_port")
    tool_roles = ("fpga_synth", "fpga_yosys", "fpga_pnr_ecp5", "fpga_pnr_ice40", "fpga_quartus_pro", "fpga_quartus_std")
    hashed_options = ("template", "part", "package", "speed", "fpga", "frontend", "defines", "synth_args", "implement")  # (staleness: these change the result)

    def inputs(self, engine) -> list[Path]:
        d = engine.project.fpga_src_dir  # (fpga_port's: the memory images; its report: what it ported for)
        return [self.filelist(engine), *self.rtl_files(engine), *self.sdc_inputs(engine), self.artifact(engine, portmod.PORT_JSON),
                *(sorted(p for p in d.rglob("*") if p.is_file()) if d.is_dir() else [])]

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
                raise ToolUnavailable(f"the `{role}` tool was not found ({hint}; see config/tools.json / `q3tui tools check`)")
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
        engine, project = ctx.engine, ctx.project
        meta = engine.state.step_meta.setdefault(self.name, {})
        run_id = getattr(engine, "_run_id", None)
        if meta.get("run") != run_id or "rounds" not in meta:  # (the rounds are per run)
            meta["run"], meta["rounds"] = run_id, 0
        meta["loop"] = False
        rep = self.reports_dir(engine)
        for n in (*REPORTS, "synth.log"):  # the last run's results: a failed synthesis leaves none behind it
            (rep / n).unlink(missing_ok=True)
        self.artifact(engine, SYNTH_JSON).unlink(missing_ok=True)
        tpl = self.template()
        try:
            ported = self.read(engine, portmod.PORT_JSON, {}) or {}
            if "fpga_port" in engine.by_name and ported and ported.get("family") != tpl.id:
                raise SynthFailed(f"the part is now {self.part()[0]} ({tpl.label}); fpga_port ported for {ported.get('family')}", f"{portmod.PART_CHANGED} {tpl.label}")
            missing = portmod.stage_inits(project, self.rtl_files(engine), tpl, self.workdir(engine))
            if missing:  # (said before the tool runs: Quartus Pro stops on it, the others synthesize an empty memory away)
                raise SynthFailed(f"the memory initialization file(s) {', '.join(missing)} the RTL names are not there (beside the RTL or in "
                                  f"{project.rel(project.fpga_src_dir)}/)", f"{tpl.tool} reads {', '.join(missing)} and they are not there")
            if tpl.backend == "yosys":
                await self._run_oss(ctx, tpl)
            elif tpl.backend == "quartus":
                await self._run_quartus(ctx, tpl)
            else:
                await self._run_vivado(ctx, tpl)
        except SynthFailed as exc:
            await self._hand_back(ctx, tpl, exc, meta)

    async def _hand_back(self, ctx: StepContext, tpl, exc: SynthFailed, meta: dict) -> None:
        """A failure goes to fpga_port as a change request; the fix loop runs it, then this step again. Out of rounds (or no fpga_port in the
        flow): the step fails with it."""
        engine, project = ctx.engine, ctx.project
        mx = self.options.get("max_rounds")
        rounds, limit = int(meta.get("rounds") or 0), int(mx) if mx is not None else ctx.cfg.pipeline.max_fix_iterations
        device = self.part()[0]
        self.write(engine, SYNTH_JSON, {"status": "failed", "error": str(exc), "template": tpl.id, "part": device, "tool": tpl.tool,
                                        "handed_to": "fpga_port" if "fpga_port" in engine.by_name and rounds < limit else None, "round": rounds + 1})
        if "fpga_port" not in engine.by_name:
            raise StepFailed(str(exc)) from exc
        if rounds >= limit:
            raise StepFailed(f"{exc} — still failing after {rounds} round(s) with fpga_port (max_rounds / pipeline.max_fix_iterations)") from exc
        text = exc.evidence if exc.evidence.startswith(portmod.PART_CHANGED) else (
            f"From fpga_synth ({tpl.tool}, {tpl.family} {device}): {exc}" + (f"\nThe tool's error lines:\n{exc.evidence}" if exc.evidence else ""))
        engine.request_change("fpga_port", text)
        meta["loop"], meta["rounds"] = True, rounds + 1
        ctx.emit("warning", message=f"synthesis failed → fpga_port (round {rounds + 1} of {limit}): {str(exc)[:300]}")

    def _failed(self, engine, res, what: str) -> SynthFailed:
        """The tool failed: its log beside the reports (fpga_port reads it there), its error lines as the evidence."""
        log = Path(res.log_path).read_text(errors="replace") if res.log_path and Path(res.log_path).is_file() else ""
        rep = self.reports_dir(engine)
        rep.mkdir(parents=True, exist_ok=True)
        (rep / "synth.log").write_text(log)
        lines = _error_lines(log) or [d.message for d in res.errors[:20]]
        errs = "; ".join(d.message for d in res.errors[:3]) or (lines[0] if lines else "no error line")
        return SynthFailed(f"{what}: {errs} (log: {engine.project.rel(rep / 'synth.log')})", "\n".join(lines))

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
            raise self._failed(engine, res, "synthesis failed")
        rep = self.reports_dir(engine)
        rep.mkdir(parents=True, exist_ok=True)
        got = []
        for n in REPORTS:
            if (work / n).is_file():
                shutil.copy2(work / n, rep / n)
                got.append(n)
        if "utilization.rpt" not in got or "timing.rpt" not in got:
            raise self._failed(engine, res, "the tool finished but wrote no utilization / timing report")
        if res.log_path and Path(res.log_path).is_file():  # (beside the reports: where you, the assistant and the judge look; .q3tui/ is hidden)
            shutil.copy2(res.log_path, rep / "synth.log")
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

    async def _run_quartus(self, ctx: StepContext, tpl) -> None:
        """Quartus Prime (the Intel families): the project, synthesis (and the fit with `implement`), the Timing Analyzer on the result; the same
        report names as Vivado's (utilization = the summary, utilization_hier = the per-entity table, timing / paths / clocks = Fmax)."""
        engine, project = ctx.engine, ctx.project
        top = self.top(engine)
        role = tpl.roles[0]
        tools = await self._need(engine, role, f"{tpl.tool}: a module such as quartus/25.1pro")
        work = self.workdir(engine)
        for n in (*REPORTS, "quartus.log", "timing.rpt", "fmax.rpt", "sta.tcl"):
            (work / n).unlink(missing_ok=True)
        for d in ("qdb", "db", "incremental_db", "output_files", "dni", "tmp-clearbox"):
            shutil.rmtree(work / d, ignore_errors=True)
        for f in work.glob(f"{quartus.PROJECT}.*"):
            f.unlink()
        device, note = self.part()
        if note:
            ctx.emit("warning", message=note)
        rtl = self.rtl_files(engine)
        portmod.stage_inits(project, rtl, tpl, work)  # (again: the clean-up above; Quartus Pro reads them in its project folder, not on the search path)
        sdc = self.sdc_inputs(engine)
        cfg = {**self._fpga_cfg(), **{k: self.options[k] for k in ("defines",) if self.options.get(k)}}
        script = project.fpga_dir / "synth.tcl"
        script.parent.mkdir(parents=True, exist_ok=True)
        project.backup([script])
        script.write_text(quartus.script_text(rtl, top, tpl, device, sdc, work, cfg))
        (work / "sta.tcl").write_text(quartus.sta_text(tpl, sdc, work, bool(cfg.get("implement"))))
        ctx.emit("log", message=f"synthesizing {top} for {tpl.family} {device} with {tpl.tool}" + (" (and fitting it)" if cfg.get("implement") else "") + "…")
        t0 = time.monotonic()
        res = await self._watched(ctx, tools, role, {"script": script, "top": top, "files": rtl, "workdir": work, "part": device}, work, "quartus.log",
                                  quartus.phase_line, "quartus", str(script))
        toolmod.check_broken(res, role)
        log = Path(res.log_path).read_text(errors="replace") if res.log_path and Path(res.log_path).is_file() else ""
        if not res.ok:
            raise self._failed(engine, res, "synthesis failed")
        stage = "fit" if cfg.get("implement") else ("syn" if tpl.edition == "pro" else "map")
        summary, report = work / f"{quartus.PROJECT}.{stage}.summary", work / f"{quartus.PROJECT}.{stage}.rpt"
        if not summary.is_file() or not (work / "timing.rpt").is_file():
            raise self._failed(engine, res, f"Quartus finished but wrote no {'summary' if not summary.is_file() else 'timing analysis'}")
        rep = self.reports_dir(engine)
        rep.mkdir(parents=True, exist_ok=True)
        files = {"utilization.rpt": summary.read_text(errors="replace"),
                 "utilization_hier.rpt": quartus.entity_table(report.read_text(errors="replace")) if report.is_file() else "",
                 "timing.rpt": (work / "timing.rpt").read_text(errors="replace"),
                 "paths.rpt": (work / "paths.rpt").read_text(errors="replace") if (work / "paths.rpt").is_file() else "",
                 "clocks.rpt": (work / "fmax.rpt").read_text(errors="replace") if (work / "fmax.rpt").is_file() else "", "synth.log": log}
        for n, text in files.items():
            (rep / n).write_text(text)
        critical = [ln.strip() for ln in log.splitlines() if ln.startswith("Critical Warning")]
        for m in critical[:3]:
            ctx.emit("warning", message=f"quartus critical warning: {m[:200]}")
        self.write(engine, SYNTH_JSON, {"tool": quartus.tool_version(log), "template": tpl.id, "backend": "quartus", "family": tpl.family, "part": device,
                                        "top": top, "mode": "in_context", "implemented": bool(cfg.get("implement")), "status": "pass",
                                        "runtime_s": round(time.monotonic() - t0, 1), "log": project.rel(Path(res.log_path)),
                                        "warnings": sum(1 for ln in log.splitlines() if ln.startswith("Warning")), "critical_warnings": len(critical),
                                        "critical_warning_messages": list(dict.fromkeys(critical))[:10], "reports": [n for n in files if n != "synth.log"]})
        ctx.emit("log", message=f"synthesis done in {time.monotonic() - t0:.0f} s ({len(critical)} critical warning(s)); reports in build/syn/fpga/reports/")

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
            raise self._failed(engine, res, "synthesis failed" if not res.ok else "synthesis wrote no netlist")
        lpf = work / "clocks.lpf"
        lpf.write_text(oss.lpf_text(clocks))
        args = oss.pnr_args(tpl, device, package, speed, js, work / "pnr.log", clocks, lpf)
        pnr = await self._watched(ctx, tools, tpl.pnr_role, {"args": args, "workdir": work}, work, "pnr.out", oss.phase_line, "nextpnr", str(js), tail_name="pnr.log")
        toolmod.check_broken(pnr, tpl.pnr_role)
        if not pnr.ok:
            raise self._failed(engine, pnr, "place and route failed (a design that does not fit the device, or needs more IOs than the package has, stops here)")
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
            raise SynthFailed(f"nextpnr reported no clock frequency (log: {pnr.log_path}): no clock net of the design matched a clock of the SDC",
                              "nextpnr: no clock net of the design matched a clock of the SDC (clocks.lpf)")
        warnings = sum(d.severity == "warning" for d in [*res.diagnostics, *pnr.diagnostics])
        self.write(engine, SYNTH_JSON, {"tool": oss.tool_version(Path(res.log_path).read_text(errors="replace") if Path(res.log_path).is_file() else ""),
                                        "template": tpl.id, "backend": "yosys", "family": tpl.family, "part": label, "device": device, "package": package, "speed": speed,
                                        "top": top, "mode": "out_of_context", "implemented": True, "status": "pass", "runtime_s": round(time.monotonic() - t0, 1),
                                        "log": project.rel(work / "pnr.log"), "warnings": warnings, "critical_warnings": 0, "critical_warning_messages": [], "reports": list(files)})
        ctx.emit("log", message=f"synthesis, place and route done in {time.monotonic() - t0:.0f} s; reports in build/syn/fpga/reports/")
