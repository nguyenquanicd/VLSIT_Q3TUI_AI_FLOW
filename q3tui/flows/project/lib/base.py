"""Base of the project settings steps: what each looks at (its inputs), and a report in `.q3tui/project/<step>.json` that a run writes. The
steps only check and report; changes are made in the views (actions in `addon.py`) and saved to the project's own files."""

from __future__ import annotations

from pathlib import Path

from q3tui.core.config import CONFIG_FILENAME
from q3tui.core.project import write_json
from q3tui.pipeline.base import StepDef


class ProjectStep(StepDef):
    watched: tuple[str, ...] = (CONFIG_FILENAME,)  # files of the project root this step's report depends on

    def inputs(self, engine) -> list[Path]:
        return [engine.project.root / n for n in self.watched if (engine.project.root / n).is_file()]

    def report_path(self, engine) -> Path:
        d = engine.project.state_dir / "project"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{self.name}.json"

    def outputs(self, engine) -> list[Path]:
        p = self.report_path(engine)
        return [p] if p.is_file() else []

    def reset_files(self, engine) -> list[Path]:
        return self.outputs(engine)

    def write_report(self, engine, data: dict) -> Path:
        return write_json(self.report_path(engine), data)
