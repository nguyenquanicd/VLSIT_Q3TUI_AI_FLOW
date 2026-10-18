"""Sanity step `sdc`: the SDC the analysis reads, shared with the FPGA and ASIC flows (`sdc/sdc.json` → `sdc/*.tcl`, code only).

Without a `sdc/sdc.json` and with a `sdc/sdc.tcl` of your own, that SDC is used as it is: nothing is generated or overwritten. Otherwise this is the
FPGA flow's `sdc` step (clocks and resets from the RTL's top and the spec, then edited through the SDC actions)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from q3tui.flows.fpga.lib import sdc as sdcmod

_f = Path(__file__).resolve().parents[2] / "fpga" / "sdc" / "step.py"
_spec = importlib.util.spec_from_file_location("q3tui_fpga_sdc_step", _f)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


class Step(_mod.Step):
    def user_sdc(self, engine) -> bool:
        return sdcmod.load(engine.project) is None and (engine.project.root / "sdc" / "sdc.tcl").is_file()

    def inputs(self, engine) -> list[Path]:
        return [f for f in (engine.project.root / "sdc").glob("*.tcl")] if self.user_sdc(engine) else super().inputs(engine)

    def outputs(self, engine) -> list[Path]:
        return sorted((engine.project.root / "sdc").glob("*.tcl")) if self.user_sdc(engine) else super().outputs(engine)

    def missing_input(self, engine) -> str | None:
        return None if self.user_sdc(engine) else super().missing_input(engine)

    async def run(self, ctx) -> None:
        if self.user_sdc(ctx.engine) and not ctx.regenerate:
            ctx.emit("log", message="sdc/sdc.tcl is yours (no sdc/sdc.json): used as it is")
            return
        await super().run(ctx)
