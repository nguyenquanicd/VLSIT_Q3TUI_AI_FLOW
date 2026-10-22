"""The analysis step of the Sanity flow (CDC, RDC): runs the tool of its role on the RTL and the shared SDC and keeps the violations as data.

The Tcl script `build/sanity/<role>_setup.tcl` is written by code each run (lib/script.py). The SDC is `constraints/sdc/sdc.tcl`: the one the FPGA / ASIC
flows render from `constraints/sdc/sdc.json`, or one you wrote. The resets come from sdc.json when there is one, else from the RTL (`options.resets` wins).
The violations are read from the report the script writes; the tool failing, or not being there, is a failed step — never a pass."""

from __future__ import annotations

import re
import time
from pathlib import Path

import anyio

from q3tui.core.project import read_json
from q3tui.eda import tools as toolmod
from q3tui.eda.base import ToolUnavailable
from q3tui.flows.sanity.lib import script, table, vcstatic
from q3tui.flows.sanity.lib.base import SanityStep, report_json
from q3tui.pipeline.base import StepContext, StepFailed


def rtl_resets(filelist: Path, root: Path, top: str) -> list[str]:
    """The reset inputs of the top, read with pyslang; empty when the RTL cannot be elaborated here (the tool then reports what is wrong)."""
    from q3tui.hdl.filelist import parse_filelist
    from q3tui.hdl.parse import parse_design

    try:
        design = parse_design(parse_filelist(filelist, root), top)
    except Exception:  # noqa: BLE001
        return []
    info = design.modules.get(top)
    if info is None:
        return []
    inputs = {p.name for p in info.ports if p.direction == "input"}
    return [r.name for r in info.resets if r.name in inputs]


class AnalysisStep(SanityStep):
    hashed_options = ("extra_tcl", "resets")  # (staleness: these change the result)

    def inputs(self, engine) -> list[Path]:
        return [self.filelist(engine), *self.rtl_files(engine), *self.sdc_files(engine)]

    def outputs(self, engine) -> list[Path]:
        return [p for p in (self.artifact(engine, report_json(self.role)), *self.table_files(engine)) if p.is_file()]

    def reset_files(self, engine) -> list[Path]:
        return self.outputs(engine)

    def missing_input(self, engine) -> str | None:
        if why := self.missing_rtl(engine):
            return why
        if self.sdc_top(engine) is None:
            return "the SDC is needed first (step sdc: constraints/sdc/sdc.tcl)"
        return None

    def script_text(self, engine, top: str, work: Path, waive: Path | None = None) -> str:
        """The run's Tcl; with `waive`, the waivers of the review sourced after the check (the `<role>_check` step)."""
        root = engine.project.root
        opts = engine.by_name[self.role].options if self.role in engine.by_name else self.options  # (the lane's analysis step owns extra_tcl / resets: the check runs the same script)
        sdcj = read_json(engine.project.sdc_dir / "sdc.json", {}) or {}
        if not sdcj.get("resets") and not opts.get("resets"):  # (a hand-written SDC says nothing of resets)
            sdcj = {**sdcj, "resets": rtl_resets(self.filelist(engine), root, top)}
        elif opts.get("resets"):
            sdcj = {**sdcj, "resets": [str(r) for r in opts["resets"]]}
        return script.text(self.role, top, root, script.absolute_filelist(self.filelist(engine), root, work), work, self.sdc_top(engine), sdcj, str(opts.get("extra_tcl") or ""), waive)

    async def run(self, ctx: StepContext) -> None:
        engine, project, role = ctx.engine, ctx.project, self.role
        top = self.top(engine)
        try:
            tools = self.tools(engine)
            if not await anyio.to_thread.run_sync(lambda: toolmod.available(tools, role)):
                raise ToolUnavailable(f"the `{role}` tool was not found (VC Static, /tools/Synopsys/vc_static; see tools.json / `q3tui tools check`)")
        except ToolUnavailable as exc:
            raise StepFailed(f"{role.upper()} not run: {exc}") from exc
        work = self.workdir(engine)
        for old in work.glob("*.rpt"):
            old.unlink()
        setup = self.setup_script(engine)
        setup.parent.mkdir(parents=True, exist_ok=True)
        project.backup([setup])
        setup.write_text(self.script_text(engine, top, work))
        ctx.emit("log", message=f"{role.upper()} analysis of {top} with {project.rel(self.sdc_top(engine))}…")
        t0 = time.monotonic()
        res = await anyio.to_thread.run_sync(lambda: toolmod.run_role(
            tools, role, work, {"script": setup, "top": top, "filelist": self.filelist(engine), "files": self.rtl_files(engine), "workdir": work}),
            abandon_on_cancel=True)
        toolmod.check_broken(res, role)
        if not res.ok:
            raise StepFailed(f"{role.upper()} failed: " + "; ".join(d.message for d in res.errors[:3]) + f" (log: {res.log_path})")
        raw = work / f"{role}_violations.rpt"
        if not raw.is_file():  # (a clean run writes one too: no report is a tool that did not get to the end, never "clean")
            raise StepFailed(f"the {role.upper()} tool finished but wrote no violation report ({raw.name}); check the report command of "
                             f"{project.rel(setup)} (log: {res.log_path})")
        found, infos = vcstatic.parse_report(raw.read_text(errors="replace"))
        if not found and vcstatic.declared(raw.read_text(errors="replace")):
            raise StepFailed(f"{role.upper()}: the report {raw.name} counts violations but none could be read from it (a format Q3TUI does not know yet)")
        violations = [{"id": f"V{i}", **v} for i, v in enumerate(found, 1)]
        self.sanity_dir(engine).joinpath(raw.name).write_text(raw.read_text(errors="replace"))
        report = {"tool": res.vendor, "top": top, "status": "clean" if not violations else "violations", "runtime_s": round(time.monotonic() - t0, 1),
                  "log": project.rel(Path(res.log_path)), "report": project.rel(self.sanity_dir(engine) / raw.name), "rtl": self.rtl_hash(engine), "sdc": self.sdc_hash(engine),
                  "infos": infos, "violations": violations}
        self.write(engine, report_json(role), report)
        table.write(self.table_files(engine), role, report, None)  # (the old review, if any, no longer fits this report: the next step decides again)
        ctx.emit("log", message=f"{role.upper()}: {len(violations)} violation(s) ({infos} info) in {time.monotonic() - t0:.0f} s; table: "
                                f"{project.rel(self.table_files(engine)[0])}")
