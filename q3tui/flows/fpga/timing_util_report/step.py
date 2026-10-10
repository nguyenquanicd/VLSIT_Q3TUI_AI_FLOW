"""FPGA step `timing_util_report`: utilization, timing (WNS / TNS / Fmax), the worst path and what is unconstrained, from the
Vivado reports, judged against the targets (`schemas/ppa_report.json`). Code only; the gate is the review of the numbers.
Setup slack is judged at every stage; hold slack only after implementation (`options.fpga.implement` of fpga_synth).

`options.targets`: `{max_util_pct: {lut: 70, ff: 70, bram_tile: 80, dsp: 80}, min_slack_ns: 0.0}` (timing must meet the SDC
by default: WNS >= `min_slack_ns`, 0 unless set)."""

from __future__ import annotations

import re
from pathlib import Path

from q3tui.pipeline.base import StepContext, StepFailed
from q3tui.flows.fpga.lib import sdc as sdcmod
from q3tui.flows.fpga.lib import vivado
from q3tui.flows.fpga.lib.base import PPA_JSON, SYNTH_JSON, FpgaStep


class Step(FpgaStep):
    name = "timing_util_report"
    title = "Timing and utilization report"
    deps = ("fpga_synth",)

    def inputs(self, engine) -> list[Path]:
        rep = self.reports_dir(engine)
        return [self.artifact(engine, SYNTH_JSON), *(rep / n for n in ("utilization.rpt", "timing.rpt", "check_timing.rpt", "paths.rpt"))]

    def outputs(self, engine) -> list[Path]:
        p = self.artifact(engine, PPA_JSON)
        return [p] if p.is_file() else []

    def reset_files(self, engine) -> list[Path]:
        return self.outputs(engine)

    def missing_input(self, engine) -> str | None:
        if not self.artifact(engine, SYNTH_JSON).is_file():
            return "the synthesis is needed first (step fpga_synth)"
        return None

    async def run(self, ctx: StepContext) -> None:
        engine, project = ctx.engine, ctx.project
        rep = self.reports_dir(engine)

        def text(name: str) -> str:
            f = rep / name
            return f.read_text(errors="replace") if f.is_file() else ""

        synth = self.read(engine, SYNTH_JSON, {})
        util, timing = vivado.parse_utilization(text("utilization.rpt")), vivado.parse_timing(text("timing.rpt"))
        if not util or "wns_ns" not in timing:
            if re.search(r"^\s*NA(\s+NA)+\s*$", text("timing.rpt"), re.M):  # the summary is all NA: no clock reached anything
                raise StepFailed("Vivado reported no timing (WNS NA): no clock constraint matched the design — check the clock / reset "
                                 "ports of the SDC against the top module (the critical warnings are in schemas/fpga_synth.json)")
            raise StepFailed("could not read the reports in fpga/reports/ (utilization.rpt / timing.rpt)")
        sdc = sdcmod.load(project)
        clocks = [{"name": c.name, "period_ns": c.period_ns, "freq_mhz": c.freq_mhz} for c in (sdc.clocks if sdc else [])]
        if clocks:  # the worst setup slack limits the clock of the (first) domain: period - WNS is the period it could run at
            period = clocks[0]["period_ns"]
            timing["fmax_mhz"] = round(1000.0 / (period - timing["wns_ns"]), 1) if period - timing["wns_ns"] > 0 else None
        targets = self.options.get("targets") or {}
        min_slack = float(targets.get("min_slack_ns", 0.0))
        over = {k: {"used_pct": util[k]["pct"], "max_pct": float(v)} for k, v in (targets.get("max_util_pct") or {}).items()
                if k in util and util[k]["pct"] > float(v)}
        implemented = bool(synth.get("implemented"))
        verdict = {"timing": "pass" if timing["wns_ns"] >= min_slack else "fail",
                   # hold needs placement and routing (before them the tool has no skew / route delay): judged only then
                   "hold": ("n/a" if "whs_ns" not in timing else ("pass" if timing["whs_ns"] >= 0 else "fail")) if implemented else "not judged (post-synthesis)",
                   "utilization": ("fail" if over else "pass") if targets.get("max_util_pct") else "n/a",
                   "over_budget": over}
        verdict["overall"] = "pass" if "fail" not in (verdict["timing"], verdict["hold"], verdict["utilization"]) else "fail"
        data = {"part": synth.get("part"), "top": synth.get("top"), "tool": synth.get("tool"), "stage": "implemented" if implemented else "post-synthesis",
                "clocks": clocks, "utilization": util, "timing": timing,
                "critical_path": vivado.critical_path(text("paths.rpt")), "unconstrained": vivado.unconstrained(text("check_timing.rpt")),
                "targets": targets, "verdict": verdict}
        self.write(engine, PPA_JSON, data)
        lut, ff = util.get("lut", {}).get("used"), util.get("ff", {}).get("used")
        ctx.emit("log", message=f"PPA ({data['stage']}): {lut} LUT, {ff} FF, WNS {timing['wns_ns']} ns"
                                + (f", Fmax ≈ {timing['fmax_mhz']} MHz" if timing.get("fmax_mhz") else "") + f" → {verdict['overall']}")
