"""EDA adapter base: roles, normalised results, subprocess runner. See docs/spec/architecture.md."""

from __future__ import annotations

import functools
import os
import shlex
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol

from q3tui.hdl.filelist import Filelist

ROLES = ("syntax", "simulator", "lint", "debug")


@dataclass
class ToolDiagnostic:
    severity: str  # error | warning | info
    code: str
    message: str
    file: str | None = None
    line: int | None = None


@dataclass
class ToolResult:
    role: str
    vendor: str
    ok: bool
    command: list[str] = field(default_factory=list)
    returncode: int | None = None
    duration_s: float = 0.0
    log_path: str | None = None
    diagnostics: list[ToolDiagnostic] = field(default_factory=list)
    timed_out: bool = False
    summary: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def errors(self) -> list[ToolDiagnostic]:
        return [d for d in self.diagnostics if d.severity == "error"]


@dataclass
class ToolRequest:
    """Inputs shared by all roles; adapters use what they need."""

    filelist: Filelist
    top: str
    extra_args: list[str] = field(default_factory=list)
    testbench_files: list[Path] = field(default_factory=list)
    script: Path | None = None  # formal TCL / lint rules etc.


class ToolAdapter(Protocol):
    role: str
    vendor: str

    def available(self) -> bool: ...

    def run(self, request: ToolRequest, workdir: Path) -> ToolResult: ...


class ToolUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class ToolEnv:
    """How to activate EDA tools: an optional setup script and/or environment modules.

    Commands run as `bash -c '<prelude> && exec <cmd>'` so `module load` works even though
    `module` is a shell function that subprocesses don't inherit.
    """

    setup_script: str | None = None
    modules: tuple[str, ...] = ()
    modules_init: str | None = None  # default: $MODULESHOME/init/bash

    def prelude(self) -> str:
        steps = []
        if self.setup_script:
            steps.append(f"source {shlex.quote(self.setup_script)} >/dev/null")
        if self.modules:
            init = self.modules_init or os.path.join(os.environ.get("MODULESHOME", "/usr/share/modules"), "init", "bash")
            steps.append(f"source {shlex.quote(init)}")
            steps.append(f"module load {shlex.join(self.modules)} >/dev/null")
        return " && ".join(steps)

    def wrap(self, cmd: list[str]) -> list[str]:
        prelude = self.prelude()
        return ["bash", "-c", f"{prelude} && exec {shlex.join(cmd)}"] if prelude else cmd

    @functools.lru_cache(maxsize=64)
    def which(self, binary: str) -> str | None:
        prelude = self.prelude()
        if not prelude:
            return shutil.which(binary)
        proc = subprocess.run(
            ["bash", "-c", f"{prelude} && command -v {shlex.quote(binary)}"],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            return None
        return proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else None


def run_command(
    cmd: list[str],
    workdir: Path,
    log_path: Path,
    timeout_s: int,
    tool_env: ToolEnv | None = None,
    env: dict[str, str] | None = None,
) -> tuple[int | None, str, float, bool]:
    """Run a tool, tee combined output to log_path. Returns (rc, output, seconds, timed_out)."""
    workdir.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    argv = tool_env.wrap(cmd) if tool_env else cmd
    start = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            cwd=workdir,
            env={**os.environ, **(env or {})},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            timeout=timeout_s,
        )
        rc, out, timed_out = proc.returncode, proc.stdout, False
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode(errors="replace")
        rc, timed_out = None, True
    except FileNotFoundError as exc:
        rc, out, timed_out = 127, f"command not found: {exc.filename}\n", False
    elapsed = time.monotonic() - start
    log_path.write_text(f"$ {shlex.join(cmd)}\n{out}")
    return rc, out, elapsed, timed_out


def compile_settled(tool, request, workdir: Path):
    """A compile that did not finish (killed, timed out — rv32im: VCS hung 600 s while several units ran it, likely a
    license wait) is the tool's problem, not the code's: run once more; the caller reports one that still does not
    finish as such, never as errors of the code it compiled."""
    result = tool.compile(request, workdir)
    if (result.timed_out or result.returncode is None) and not result.errors:
        result = tool.compile(request, workdir)
    return result


def unfinished(result) -> bool:
    """The tool never finished (no exit code / timed out) and reported no error: nothing to blame on the code."""
    return (result.timed_out or result.returncode is None) and not result.errors
