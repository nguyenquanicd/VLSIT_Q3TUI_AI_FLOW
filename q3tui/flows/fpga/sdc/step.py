"""FPGA step `sdc`: `sdc/{clocks,clock_groups,exceptions,io_constraints,env,sdc}.tcl` from `sdc/sdc.json` (code only, no LLM).

First run: sdc.json is made from the top module's ports (clock = input named like a clock, resets = false paths) and the
clock the spec states ("target frequency 100 MHz"; `options.period_ns` wins, else 10 ns). After that sdc.json is yours: edit it
(or the TUI's SDC tab) and the step renders the Tcl files again."""

from __future__ import annotations

from pathlib import Path

import anyio

from q3tui.pipeline.base import StepContext, StepFailed
from q3tui.flows.fpga.lib import sdc as sdcmod
from q3tui.flows.fpga.lib.base import FpgaStep


class Step(FpgaStep):
    name = "sdc"
    title = "SDC constraints"
    deps = ()

    def inputs(self, engine) -> list[Path]:
        p = engine.project
        cfg = sdcmod.config_path(p)
        return [self.filelist(engine), *self.rtl_files(engine), *p.spec_documents(), *([cfg] if cfg.is_file() else [])]

    def outputs(self, engine) -> list[Path]:
        return self.sdc_inputs(engine)

    def reset_files(self, engine) -> list[Path]:
        return [f for f in self.outputs(engine)]  # sdc.json is yours: a reset keeps it

    def missing_input(self, engine) -> str | None:
        return self.missing_rtl(engine)

    async def run(self, ctx: StepContext) -> None:
        engine, project = ctx.engine, ctx.project
        top = self.top(engine)
        try:
            ports = await anyio.to_thread.run_sync(lambda: sdcmod.top_ports(project, self.filelist(engine), top))  # (pyslang)
        except ValueError as exc:
            raise StepFailed(str(exc)) from exc
        cfg = sdcmod.load(project)
        if cfg is None or ctx.regenerate:
            text = "\n".join(p.read_text(errors="replace") for p in project.spec_documents() if p.suffix.lower() in (".md", ".txt", ".rst"))
            cfg = sdcmod.default_config(ports, text, self.options.get("period_ns"))
            sdcmod.save(project, cfg)
            ctx.emit("log", message="sdc/sdc.json created: " + (", ".join(f"{c.name} {c.freq_mhz:g} MHz" for c in cfg.clocks) or "no clock found"))
        if not cfg.clocks:
            raise StepFailed("no clock: none of the top module's inputs is named like one; add one to sdc/sdc.json (SDC tab)")
        problems = sdcmod.port_problems(cfg, ports)
        if problems:  # the RTL's ports changed (or the SDC names a port that is not there): hand it back, never constrain nothing
            raise StepFailed(f"the SDC does not match the ports of {top}: " + "; ".join(problems)
                             + ". Fix it in the SDC view (e on the clock: its port) or sdc/sdc.json, or `q3tui act sdc_set key=clock.<name>.port value=<port>`.")
        try:
            written = sdcmod.write(project, cfg, ports)
        except ValueError as exc:
            raise StepFailed(f"sdc/sdc.json: {exc}") from exc
        ctx.emit("log", message=f"SDC: {', '.join(f'{c.name} {c.period_ns:g} ns ({c.freq_mhz:g} MHz)' for c in cfg.clocks)}; "
                                f"{len(written)} file(s) written")
        ctx.unchanged = not written
