"""ASIC step `asic_synth`: Design Compiler on the RTL with the SDC and a standard-cell library (tool role `asic_synth`).

The Tcl script is written by code (`lib/dc.py`); the netlist goes to `build/syn/asic/netlist/`, the reports to `build/syn/asic/reports/`, the numbers
(area, worst slack, power, violations) and the verdict against `min_slack_ns` / `max_area_um2` to `schemas/asic_synth.json`.
A missing tool or library is a failed step ("not run"), never a pass."""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path

import anyio

from q3tui.eda import tools as toolmod
from q3tui.eda.base import ToolUnavailable
from q3tui.flows.asic.lib import dc
from q3tui.flows.asic.lib.base import REPORTS, SYNTH_JSON, AsicStep
from q3tui.flows.fpga.lib import hints
from q3tui.flows.fpga.lib import sdc as sdcmod
from q3tui.pipeline.base import StepContext, StepFailed


def _version(log: str | None) -> str:
    m = re.search(r"Version:\s+(\S+)", Path(log).read_text(errors="replace")) if log and Path(log).is_file() else None
    return f"Design Compiler {m.group(1)}" if m else "Design Compiler"


class Step(AsicStep):
    name = "asic_synth"
    title = "ASIC synthesis"
    deps = ("asic_port", "sdc")
    tool_roles = ("asic_synth",)
    hashed_options = ("library", "compile", "max_paths", "extra_tcl", "defines", "min_slack_ns", "max_area_um2")  # (staleness: these change the result)

    def inputs(self, engine) -> list[Path]:
        # (sdc.json too: the ASIC's own clock frequencies are in it, not in the shared Tcl files)
        return [self.filelist(engine), *self.rtl_files(engine), *self.sdc_inputs(engine), sdcmod.config_path(engine.project)]

    def outputs(self, engine) -> list[Path]:
        rep, net = self.reports_dir(engine), self.netlist(engine)
        files = [rep / n for n in REPORTS if (rep / n).is_file()]
        return [*([net] if net.is_file() else []), *files, *([self.artifact(engine, SYNTH_JSON)] if self.artifact(engine, SYNTH_JSON).is_file() else [])]

    def reset_files(self, engine) -> list[Path]:
        return self.outputs(engine)

    def missing_input(self, engine) -> str | None:
        return self.missing_rtl(engine)

    def _constraints(self, ctx: StepContext) -> tuple[Path | None, dict[str, float], "sdcmod.SdcConfig | None"]:
        """(the SDC file to source, the clocks with their own ASIC frequency {name: MHz}, the constraints in effect). When no clock has one that is
        the shared `constraints/sdc/sdc.tcl`; otherwise `constraints/sdc/sdc.json` with those clocks changed is rendered into `build/syn/asic/sdc/` — the FPGA's SDC stays as it is.
        The ASIC frequency of a clock is set in the SDC tab (*ASIC MHz*) or `sdc_set key=clock.<name>.asic_freq_mhz`."""
        engine, project = ctx.engine, ctx.project
        cfg = sdcmod.load(project)
        shared = sdcmod.sdc_dir(project) / "sdc.tcl"
        want = {c.name: c.asic_freq_mhz for c in (cfg.clocks if cfg else []) if c.asic_freq_mhz}
        if not want:
            return (shared if shared.is_file() else None), {}, cfg
        cfg = cfg.model_copy(deep=True)
        for name, mhz in want.items():
            sdcmod.apply_edit(cfg, f"clock.{name}.freq_mhz", str(mhz))
        ports = sdcmod.top_ports(project, self.filelist(engine), self.top(engine))
        out = self.asic_dir(engine) / "sdc"
        out.mkdir(parents=True, exist_ok=True)
        for fname, text in sdcmod.render(cfg, ports).items():
            (out / fname).write_text(text)
        return out / "sdc.tcl", want, cfg

    async def run(self, ctx: StepContext) -> None:
        engine, project = ctx.engine, ctx.project
        top = self.top(engine)
        libs = self.libraries(engine)
        if not libs:
            raise StepFailed("ASIC synthesis not run: no library — set `library` (the .db of the standard cells) in Settings → asic_synth")
        missing = [x for x in libs if not Path(x).is_file()]
        if missing:
            raise StepFailed(f"ASIC synthesis not run: library not found: {', '.join(missing)} (Settings → asic_synth)")
        try:
            tools = self.tools(engine)
            if not await anyio.to_thread.run_sync(lambda: toolmod.available(tools, "asic_synth")):
                raise ToolUnavailable("the `asic_synth` tool was not found (module synopsys/syn; see tools.json / `q3tui tools check`)")
        except ToolUnavailable as exc:
            raise StepFailed(f"ASIC synthesis not run: {exc}") from exc
        work = self.workdir(engine)
        for n in (*REPORTS, "dc.log", "dc.out"):
            (work / n).unlink(missing_ok=True)
        netlist = self.netlist(engine)
        script = self.asic_dir(engine) / "synth.tcl"
        script.parent.mkdir(parents=True, exist_ok=True)
        project.backup([script])
        sdc, overridden, sdc_cfg = self._constraints(ctx)
        script.write_text(dc.script_text(self.rtl_files(engine), top, libs, sdc, work, netlist, self.options))
        ctx.emit("log", message=f"synthesizing {top} with {Path(libs[0]).name}"
                                + (f" at {', '.join(f'{n} {m:g} MHz' for n, m in overridden.items())} (its own ASIC clock)" if overridden else "") + "…")
        t0 = time.monotonic()
        done = anyio.Event()

        async def watch() -> None:
            """The tool writes {workdir}/dc.log as it goes: its phases are the progress; a quiet minute gets a heartbeat."""
            tail, quiet = dc.LogTail(work / "dc.log"), 0.0
            while not done.is_set():
                with anyio.move_on_after(2):
                    await done.wait()
                shown = [p for p in map(dc.phase_line, tail.new()) if p]
                for p in shown[-3:]:
                    ctx.emit("log", message=f"dc: {p}")
                quiet = 0.0 if shown else quiet + 2
                if quiet >= 30:
                    ctx.emit("log", message=f"dc: still running ({time.monotonic() - t0:.0f} s)…")
                    quiet = 0.0

        finished = False
        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(watch)
                try:
                    res = await anyio.to_thread.run_sync(lambda: toolmod.run_role(
                        tools, "asic_synth", work, {"script": script, "top": top, "filelist": self.filelist(engine), "files": self.rtl_files(engine),
                                                    "workdir": work}, log_name="dc.out"), abandon_on_cancel=True)
                    finished = True
                finally:
                    done.set()
        finally:
            if not finished:  # stopped (x) or failed: the thread is let go, so the tool must not be left running
                with anyio.CancelScope(shield=True):
                    await anyio.to_thread.run_sync(lambda: subprocess.run(["pkill", "-TERM", "-f", str(script)], capture_output=True))
        toolmod.check_broken(res, "asic_synth")
        if not res.ok:
            errs = "; ".join(d.message for d in res.errors[:3])
            raise StepFailed(f"synthesis failed: {errs} (log: {res.log_path})")
        rep = self.reports_dir(engine)
        rep.mkdir(parents=True, exist_ok=True)
        for n in REPORTS:
            if (work / n).is_file():
                shutil.copy2(work / n, rep / n)
        if not netlist.is_file() or not (rep / "area.rpt").is_file():
            raise StepFailed(f"the tool finished but wrote no netlist / area report (log: {res.log_path})")

        def text(name: str) -> str:
            f = rep / name
            return f.read_text(errors="replace") if f.is_file() else ""

        area, timing, qor, power = dc.parse_area(text("area.rpt")), dc.parse_timing(text("timing.rpt")), dc.parse_qor(text("qor.rpt")), dc.parse_power(text("power.rpt"))
        viol, checks, unconstrained = dc.violations(text("constraints.rpt")), dc.check_design(text("check_design.rpt")), dc.unconstrained(text("check_timing.rpt"))
        min_slack = float(self.options.get("min_slack_ns") or 0.0)
        max_area = self.options.get("max_area_um2")
        slack = timing.get("slack_ns", qor.get("wns_ns"))
        verdict = {"timing": "n/a" if slack is None else ("pass" if slack >= min_slack else "fail"),
                   "area": "n/a" if not max_area else ("pass" if area.get("total_um2", 0) <= float(max_area) else "fail"),
                   "constraints": "fail" if viol else "pass"}
        verdict["overall"] = "fail" if "fail" in verdict.values() else "pass"
        groups = dc.parse_groups(text("qor.rpt"), {c.name: c.period_ns for c in (sdc_cfg.clocks if sdc_cfg else [])})
        fmax = min((g["fmax_mhz"] for g in groups if g["fmax_mhz"]), default=None)  # the slowest clock limits what the design is good for
        self.write(engine, SYNTH_JSON, {
            "tool": _version(res.log_path), "top": top, "library": libs, "status": "pass", "runtime_s": round(time.monotonic() - t0, 1),
            "log": project.rel(Path(res.log_path)), "netlist": project.rel(netlist), "svf": project.rel(work / f"{top}.svf"),
            "clocks": [{"name": c.name, "period_ns": c.period_ns, "freq_mhz": c.freq_mhz} for c in (sdc_cfg.clocks if sdc_cfg else [])],
            "clock_override": overridden, "rtl_sha": hints.rtl_sha(project), "area": area, "timing": {**timing, **{k: v for k, v in qor.items() if k not in timing}, "fmax_mhz": fmax, "groups": groups}, "power": power,
            "violation_count": len(viol), "violations": viol[:20], "check_design": checks, "unconstrained": unconstrained,
            "targets": {"min_slack_ns": min_slack, "max_area_um2": max_area}, "verdict": verdict})
        shutil.copy2(work / f"{top}.svf", self.asic_dir(engine) / "netlist" / f"{top}.svf") if (work / f"{top}.svf").is_file() else None
        ctx.emit("log", message=f"ASIC: area {area.get('total_um2')} µm², {area.get('cells')} cells, slack {slack} ns{f' (expected Fmax {'≥' if all(g.get('bound') == 'at_least' for g in groups) else '≈'} {fmax:g} MHz in this library, target ' + ', '.join(f'{g["target_mhz"]:g}' for g in groups) + ' MHz)' if fmax else ''}, power {power.get('total_uw')} µW → {verdict['overall']}")
