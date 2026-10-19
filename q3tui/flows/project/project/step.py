"""Step `project`: the project's configuration as it is — the flow, the folders, the files that configure it — and what is wrong with it."""

from __future__ import annotations

from pathlib import Path

from q3tui import flows
from q3tui.flows.project.lib.base import ProjectStep
from q3tui.pipeline.base import StepContext


class Step(ProjectStep):
    name = "project"
    title = "Project"
    watched = ("q3tui.yaml", "tools.json", "pdks.json")

    async def run(self, ctx: StepContext) -> None:
        project = ctx.project
        cfg = project.cfg
        problems = []
        try:
            flows.find_flow(cfg.pipeline.flow, project.root)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"pipeline.flow: {exc}")
        folders = {n: str(getattr(cfg.project, n)) for n in ("spec_dir", "req_dir", "schemas_dir", "src_dir", "docs_dir", "state_dir")}
        files = {n: (project.root / n).is_file() for n in ("q3tui.yaml", "tools.json", "pdks.json")}
        self.write_report(ctx.engine, {"root": str(project.root), "flow": cfg.pipeline.flow, "folders": folders, "files": files, "problems": problems,
                                       "config": project.rel(Path(cfg.source)) if getattr(cfg, "source", "") else "(defaults)"})
        for p in problems:
            ctx.emit("warning", message=p)
        ctx.emit("log", message=f"project {project.root.name}: flow {cfg.pipeline.flow}, {sum(files.values())} config file(s)" + (f", {len(problems)} problem(s)" if problems else ""))
