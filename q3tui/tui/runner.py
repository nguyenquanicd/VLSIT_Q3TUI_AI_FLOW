"""Background runs: a run the TUI starts is a `q3tui run` process of its own, so it goes on while you look at another flow (the
TUI restarts on a flow switch) and ends when you stop the TUI. The TUI follows it the way it follows any run of another process
(run.lock, runs/*/events.jsonl: `Q3TUIApp._tick`); the one `Runner` outlives the app instances of a `q3tui tui` session."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path


class Runner:
    def __init__(self) -> None:
        self.child: subprocess.Popen | None = None
        self.flow: str | None = None

    def alive(self) -> bool:
        return self.child is not None and self.child.poll() is None

    @property
    def pid(self) -> int | None:
        return self.child.pid if self.alive() else None

    def command(self, project, flow: str, start=None, stop=None, only=None, regenerate=()) -> list[str]:
        """`q3tui run` for what the TUI would run in-process: the live model / effort and auto switches go along (a setting changed
        for this session only is not in q3tui.yaml)."""
        cfg = project.cfg
        cmd = [sys.executable, "-m", "q3tui.core.cli", "-C", str(project.root), "--flow", flow,
               "--model", cfg.llm.model, "--effort", cfg.llm.effort or "none", "run"]
        for flag, value in (("--from", start), ("--to", stop), ("--only", only)):
            if value:
                cmd += [flag, value]
        for step in sorted(regenerate or ()):
            cmd += ["--regenerate", step]
        if cfg.pipeline.auto_answer:
            cmd.append("--auto-answer")
        elif cfg.pipeline.auto_approve:
            cmd.append("--auto-approve")
        cmd.append("--spec-review" if cfg.spec.self_review else "--no-spec-review")
        return cmd

    def start(self, project, flow: str, **kw) -> int:
        log = project.ensure_state_dir() / "tui" / "run.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("ab") as out:
            self.child = subprocess.Popen(self.command(project, flow, **kw), cwd=project.root, stdin=subprocess.DEVNULL, stdout=out,
                                          stderr=out, start_new_session=True, env={**os.environ, "NO_COLOR": "1", "COLUMNS": "200"})
        self.flow = flow
        return self.child.pid

    def stop(self) -> bool:
        """Ctrl-C to the run: it cancels the step (its tools are killed by the step) and records the run as cancelled."""
        if not self.alive():
            return False
        os.kill(self.child.pid, signal.SIGINT)
        return True

    def shutdown(self, wait: float = 20.0) -> None:
        """The TUI ends: so does its run. Gently first (the step cleans up what it started), then for sure (its whole group)."""
        if not self.alive():
            return
        self.stop()
        end = time.monotonic() + wait
        while self.alive() and time.monotonic() < end:
            time.sleep(0.2)
        if self.alive():
            try:
                os.killpg(self.child.pid, signal.SIGKILL)  # (its own session: Vivado and friends go too)
            except ProcessLookupError:
                pass
            self.child.wait()


_RUNNER = Runner()


def get() -> Runner:
    return _RUNNER
