"""Shared pieces of the Sanity flow steps: the RTL (the VLSIT flow's result, read only), the SDC (shared with the FPGA and ASIC flows' files in
`constraints/sdc/`), where the setup scripts, the reports and the decisions go."""

from __future__ import annotations

from pathlib import Path

from q3tui.core.project import sha256_json
from q3tui.flows.fpga.lib.base import FpgaStep

ROLES = ("cdc", "rdc")


def report_json(role: str) -> str:
    return f"{role}_report.json"  # schemas/: what the tool found (tool, top, status, violations)


def waivers_json(role: str) -> str:
    return f"{role}_waivers.json"  # schemas/: the decision for every violation


def check_json(role: str) -> str:
    return f"{role}_check.json"  # schemas/: the run with the waivers applied against what the review decided


class SanityStep(FpgaStep):
    """Base of the Sanity flow's steps (the RTL / SDC / artifact helpers are the FPGA flow's: the same files)."""

    role = ""  # "cdc" | "rdc"

    def workdir(self, engine) -> Path:
        d = engine.project.state_dir / "sanity" / self.name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def sanity_dir(self, engine) -> Path:
        return engine.project.build_dir / "sanity"  # the setup / waive scripts and the raw tool reports (disposable)

    def setup_script(self, engine) -> Path:
        return self.sanity_dir(engine) / f"{self.role}_setup.tcl"

    def waive_script(self, engine) -> Path:
        return self.sanity_dir(engine) / f"{self.role}_waive.tcl"

    def table_files(self, engine) -> list[Path]:
        """The violation table (Markdown for reading, CSV for a spreadsheet)."""
        return [engine.project.reports_dir / f"{self.role}_report.{ext}" for ext in ("md", "csv")]

    def rtl_hash(self, engine) -> str:
        return sha256_json({f.name: f.read_text(errors="replace") for f in self.rtl_files(engine)})[:12]

    def sdc_files(self, engine) -> list[Path]:
        """Every Tcl file of `constraints/sdc/`: the ones the FPGA flow renders from sdc.json, or the ones you wrote (sdc.tcl sources the others)."""
        d = engine.project.sdc_dir
        return sorted(d.glob("*.tcl")) if d.is_dir() else []

    def sdc_hash(self, engine) -> str:
        return sha256_json({f.name: f.read_text(errors="replace") for f in self.sdc_files(engine)})[:12]

    def sdc_top(self, engine) -> Path | None:
        f = engine.project.sdc_dir / "sdc.tcl"
        return f if f.is_file() else None
