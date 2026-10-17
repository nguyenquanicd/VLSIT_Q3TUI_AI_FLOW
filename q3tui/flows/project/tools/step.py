"""Step `tools`: which tool roles exist, where each comes from, its modules, and whether the tool is found (a missing tool is reported, not a
failure of this step: this flow is for looking and setting)."""

from __future__ import annotations

import anyio

from q3tui.flows.project.lib import common
from q3tui.flows.project.lib.base import ProjectStep
from q3tui.pipeline.base import StepContext


class Step(ProjectStep):
    name = "tools"
    title = "Tools"
    watched = ("q3tui.yaml", "tools.json", "tools.yaml")

    async def run(self, ctx: StepContext) -> None:
        rows = await anyio.to_thread.run_sync(lambda: common.tool_rows(ctx.project))  # (a shell per role: `module load`)
        common.save_tool_rows(ctx.project, rows)
        missing = [r["role"] for r in rows if not r["found"]]
        self.write_report(ctx.engine, {"roles": len(rows), "found": len(rows) - len(missing), "missing": missing})
        for r in missing:
            ctx.emit("warning", message=f"tool role '{r}' is not found")
        ctx.emit("log", message=f"{len(rows) - len(missing)} of {len(rows)} tool role(s) found" + (f"; missing: {', '.join(missing)}" if missing else ""))
