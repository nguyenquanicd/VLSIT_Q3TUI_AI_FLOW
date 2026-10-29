"""Step `fpga`: the FPGA devices the FPGA flow can synthesize for (the templates' devices, yours in config/fpgas.json, the parts the spec names)
and the one in use (fpga_synth's template + part in q3tui.yaml): whether it is the spec's, and whether the tools of its template are here."""

from __future__ import annotations

from pathlib import Path

from q3tui.flows.fpga.lib import devices, templates
from q3tui.flows.project.lib.base import ProjectStep
from q3tui.pipeline.base import StepContext


class Step(ProjectStep):
    name = "fpga"
    title = "FPGA"
    watched = ("q3tui.yaml", devices.FILE)

    def inputs(self, engine) -> list[Path]:
        return [*super().inputs(engine), *engine.project.spec_documents()]  # (the spec's part is checked too)

    async def run(self, ctx: StepContext) -> None:
        from q3tui.eda.base import ToolUnavailable
        from q3tui.eda.tools import available, load_tools

        project = ctx.project
        use = devices.in_use(project)
        tpl = templates.TEMPLATES.get(use["template"] or "")
        problems = devices.problems(project)
        roles = tpl.roles if tpl else []
        try:
            tools = load_tools(project.cfg, project.root)
            missing = [r for r in roles if r not in tools.roles or not available(tools, r)]
        except ToolUnavailable as exc:
            missing = roles
            problems.append(str(exc))
        if missing:
            problems.append(f"{tpl.title} needs the tool role(s) {', '.join(missing)}: not found (the Tools step)")
        self.write_report(ctx.engine, {"in_use": use, "family": tpl.id if tpl else None, "spec_parts": devices.spec_parts(project), "problems": problems,
                                       "devices": len(devices.entries(project))})
        for p in problems:
            ctx.emit("warning", message=p)
        ctx.emit("log", message=f"FPGA in use: {use['device']} ({tpl.label if tpl else 'no family here synthesizes it'})" + ("" if use["set"] else " — not chosen: the default"))
