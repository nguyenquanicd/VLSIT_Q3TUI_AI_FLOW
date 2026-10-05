"""Synopsys adapters. M0: VCS (compile + run). VC Formal, VC SpyGlass, Verdi follow in M1-M4."""

from __future__ import annotations

import re
from pathlib import Path

from q3tui.core.config import ToolBinding
from q3tui.eda.base import ToolDiagnostic, ToolEnv, ToolRequest, ToolResult, run_command

# Error-[SE] Syntax error
#   Following verilog source has syntax error :
#   "rtl/foo.sv", 12: token is 'endmodule'
_VCS_HDR_RE = re.compile(r"^(Error|Warning|Lint|Note)-\[([\w-]+)\]\s*(.*)$")
_VCS_LOC_RE = re.compile(r'^\s*"?([^",\s]+\.\w+)"?,\s*(\d+)')
_VCS_WRAPPED_LOC_RE = re.compile(r'"([^"\s]+\.\w+)",\s*(\d+)')
_UVM_ERR_RE = re.compile(r"^(UVM_ERROR|UVM_FATAL)\s+(\S+)\((\d+)\)\s*@\s*[^:]*:\s*(.*)$")
_SV_ERR_RE = re.compile(r'^(Error|Fatal):\s*"?([^",\s]+)"?,\s*(\d+)[:,]?\s*(.*)$')

_SEVERITY = {"Error": "error", "Warning": "warning", "Lint": "warning", "Note": "info"}


def parse_vcs_log(text: str) -> list[ToolDiagnostic]:
    diags: list[ToolDiagnostic] = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = _VCS_HDR_RE.match(line)
        if m:
            kind, code, title = m.groups()
            file, lineno, detail = None, None, []
            for follow in lines[i + 1 : i + 8]:
                if not follow.strip() or _VCS_HDR_RE.match(follow):
                    break
                loc = _VCS_LOC_RE.match(follow)
                if loc and file is None:
                    file, lineno = loc.group(1), int(loc.group(2))
                detail.append(follow.strip())
            if file is None and detail:  # a long path wraps: `"/very/long/path.sv", ⏎ 407: token is …`
                wrapped = _VCS_WRAPPED_LOC_RE.search(" ".join(detail))
                if wrapped:
                    file, lineno = wrapped.group(1), int(wrapped.group(2))
            message = title + (" — " + " ".join(detail) if detail else "")
            diags.append(ToolDiagnostic(_SEVERITY[kind], code, message[:500], file, lineno))
            continue
        m = _UVM_ERR_RE.match(line)
        if m:
            diags.append(ToolDiagnostic("error", m.group(1), m.group(4)[:500], m.group(2), int(m.group(3))))
            continue
        m = _SV_ERR_RE.match(line)
        if m:
            diags.append(ToolDiagnostic("error", "RUNTIME", m.group(4)[:500], m.group(2), int(m.group(3))))
    return diags


class VcsSimulator:
    role = "simulator"
    vendor = "synopsys"

    def __init__(self, binding: ToolBinding, timeout_s: int, tool_env: ToolEnv | None = None):
        self.bin = binding.bin or "vcs"
        self.extra_args = binding.extra_args
        self.timeout_s = timeout_s
        self.tool_env = tool_env or ToolEnv()

    def available(self) -> bool:
        return self.tool_env.which(self.bin) is not None

    def compile_command(self, request: ToolRequest) -> list[str]:
        fl = request.filelist
        cmd = [self.bin, "-full64", "-sverilog", "-timescale=1ns/1ps", "-top", request.top, "-o", "simv"]
        cmd += [f"+incdir+{d}" for d in fl.incdirs]
        cmd += [f"+define+{k}={v}" if v is not None else f"+define+{k}" for k, v in fl.defines.items()]
        cmd += [a for f in fl.lib_files for a in ("-v", str(f))]
        cmd += [a for d in fl.lib_dirs for a in ("-y", str(d))]
        cmd += [f"+libext+{'+'.join(fl.lib_exts)}"] if fl.lib_exts else []
        cmd += [*self.extra_args, *request.extra_args]
        cmd += [str(f) for f in fl.files] + [str(f) for f in request.testbench_files]
        return cmd

    def compile(self, request: ToolRequest, workdir: Path) -> ToolResult:
        cmd = self.compile_command(request)
        log = workdir / "compile.log"
        rc, out, secs, timed_out = run_command(cmd, workdir, log, self.timeout_s, self.tool_env)
        diags = parse_vcs_log(out)
        ok = rc == 0 and not timed_out and not any(d.severity == "error" for d in diags)
        n_err = sum(d.severity == "error" for d in diags)
        return ToolResult(self.role, self.vendor, ok, cmd, rc, secs, str(log), diags, timed_out, f"vcs compile: rc={rc}, {n_err} errors")

    def simulate(self, workdir: Path, plusargs: list[str] | None = None) -> ToolResult:
        cmd = [str(workdir / "simv"), *(plusargs or [])]
        log = workdir / "sim.log"
        rc, out, secs, timed_out = run_command(cmd, workdir, log, self.timeout_s, self.tool_env)
        diags = parse_vcs_log(out)
        ok = rc == 0 and not timed_out and not any(d.severity == "error" for d in diags)
        return ToolResult(self.role, self.vendor, ok, cmd, rc, secs, str(log), diags, timed_out, f"simv: rc={rc}, {len(diags)} diagnostics")

    def run(self, request: ToolRequest, workdir: Path) -> ToolResult:
        result = self.compile(request, workdir)
        return self.simulate(workdir) if result.ok else result
