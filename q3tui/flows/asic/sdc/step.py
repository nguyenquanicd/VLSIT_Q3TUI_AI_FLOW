"""ASIC step `sdc`: the FPGA flow's SDC step (the same `sdc/sdc.json` → `sdc/*.tcl`, code only): both flows work on the same files."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_f = Path(__file__).resolve().parents[2] / "fpga" / "sdc" / "step.py"
_spec = importlib.util.spec_from_file_location("q3tui_fpga_sdc_step", _f)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


class Step(_mod.Step):
    pass
