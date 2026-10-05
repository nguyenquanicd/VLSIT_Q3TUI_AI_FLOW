"""`syntax` role via pyslang — always available, no licence needed."""

from __future__ import annotations

import time
from pathlib import Path

from q3tui.eda.base import ToolDiagnostic, ToolRequest, ToolResult
from q3tui.hdl.filelist import Filelist
from q3tui.hdl.parse import parse_design


class SlangSyntax:
    role = "syntax"
    vendor = "slang"

    def available(self) -> bool:
        return True

    def run(self, request: ToolRequest, workdir: Path) -> ToolResult:
        fl = request.filelist
        if request.testbench_files:
            fl = Filelist(
                files=[*fl.files, *request.testbench_files],
                incdirs=fl.incdirs,
                defines=fl.defines,
                lib_files=fl.lib_files,
            )
        start = time.monotonic()
        design = parse_design(fl, request.top or None)
        result = ToolResult(
            role=self.role,
            vendor=self.vendor,
            ok=design.ok,
            command=["pyslang", *map(str, fl.all_sources())],
            returncode=0 if design.ok else 1,
            duration_s=time.monotonic() - start,
            diagnostics=[ToolDiagnostic(d.severity, d.code, d.message, d.file, d.line) for d in design.diagnostics],
        )
        n_err = len(result.errors)
        result.summary = f"slang: {n_err} errors, {len(result.diagnostics) - n_err} warnings, top={design.top or '?'}"
        return result
