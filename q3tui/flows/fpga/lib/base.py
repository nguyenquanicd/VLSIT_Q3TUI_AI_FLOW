"""Shared pieces of the FPGA flow steps: where the RTL, the SDC and the results are, the tool environment."""

from __future__ import annotations

from pathlib import Path

from q3tui.core.project import read_json, write_json
from q3tui.pipeline.base import StepDef
from q3tui.flows.fpga.lib import sdc as sdcmod

SYNTH_JSON = "fpga_synth.json"  # schemas/: what the synthesis run produced (tool, part, status, reports)
PPA_JSON = "ppa_report.json"  # schemas/: utilization + timing + verdict against the targets


class FpgaStep(StepDef):
    """Base of the FPGA flow's steps. The RTL is the VLSIT flow's result (`src/rtl/`, read only); the constraints are `sdc/`."""

    def rtl_dir(self, engine) -> Path:
        return engine.project.src_dir / "rtl"

    def filelist(self, engine) -> Path:
        return engine.project.root / (self.options.get("filelist") or str(self.rtl_dir(engine).relative_to(engine.project.root) / "filelist.f"))

    def rtl_files(self, engine) -> list[Path]:
        d = self.rtl_dir(engine)
        return sorted(d.glob("*.sv")) + sorted(d.glob("*.v")) if d.is_dir() else []

    def top(self, engine) -> str | None:
        if self.options.get("top"):
            return str(self.options["top"])
        spec = read_json(engine.project.schemas_dir / "structured_spec.json", {}) or {}
        return (spec.get("metadata") or {}).get("top_module")

    def missing_rtl(self, engine) -> str | None:
        if not self.filelist(engine).is_file():
            return f"the RTL is needed first ({engine.project.rel(self.filelist(engine))}; run the vlsit flow's rtl step)"
        if not self.top(engine):
            return "no top module: set `options.top` (or run the vlsit flow's parse step: schemas/structured_spec.json)"
        return None

    def workdir(self, engine) -> Path:
        d = engine.project.state_dir / "fpga" / self.name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def reports_dir(self, engine) -> Path:
        return engine.project.root / "fpga" / "reports"

    def sdc_inputs(self, engine) -> list[Path]:
        d = sdcmod.sdc_dir(engine.project)
        return [d / n for n in sdcmod.FILES if (d / n).is_file()]

    def tools(self, engine):
        from q3tui.eda import tools as toolmod

        return toolmod.load_tools(engine.project.cfg, engine.project.root, engine.flow)

    def artifact(self, engine, name: str) -> Path:
        return engine.project.schemas_dir / name

    def read(self, engine, name: str, default=None):
        return read_json(self.artifact(engine, name), default)

    def write(self, engine, name: str, data: dict) -> Path:
        return write_json(self.artifact(engine, name), data)
