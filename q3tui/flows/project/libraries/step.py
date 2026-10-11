"""Step `libraries`: the catalog of ASIC standard-cell libraries (built-in open-source ones + the project's pdks.json) and the library in use:
what exists here, what does not, and whether the chosen library file is there."""

from __future__ import annotations

from q3tui.flows.asic.lib import pdks
from q3tui.flows.fpga.lib import hints
from q3tui.flows.project.lib.base import ProjectStep
from q3tui.pipeline.base import StepContext


class Step(ProjectStep):
    name = "libraries"
    title = "Libraries"
    watched = ("q3tui.yaml", "pdks.json")

    async def run(self, ctx: StepContext) -> None:
        project = ctx.project
        entries = pdks.entries(project)
        problems = [f"technology '{e['id']}' ({e['source']}) matches no .db file here" for e in entries if not e["files"]]
        lib = hints.asic_library(project)
        from pathlib import Path

        if lib and not Path(lib).is_file():
            problems.append(f"the ASIC library {lib} does not exist")
        self.write_report(ctx.engine, {"library": lib, "technologies": [{"id": e["id"], "source": e["source"], "corners": len(e["files"])} for e in entries], "problems": problems})
        for p in problems:
            ctx.emit("warning", message=p)
        ctx.emit("log", message=f"{sum(1 for e in entries if e['files'])} of {len(entries)} technologies have libraries here; in use: {pdks.short_name(lib, project) if lib else 'none'}")
