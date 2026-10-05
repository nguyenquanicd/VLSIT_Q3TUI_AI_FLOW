"""Persisted pipeline state: .q3tui/pipeline.json."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class StepRecord(BaseModel):
    status: Literal["pending", "running", "done", "failed"] = "pending"
    origin: Literal["generated", "user"] | None = None
    inputs_hash: str | None = None
    outputs: dict[str, str] = Field(default_factory=dict)  # relpath -> sha256 at completion
    run_id: str | None = None
    finished: str | None = None
    error: str | None = None
    cost_usd: float = 0.0


class GateRecord(BaseModel):
    status: Literal["open", "approved"] = "open"
    approved_at: str | None = None
    # a step without a configured review gate asked blocking question(s): it waits for you this once
    for_questions: bool = False


class PipelineState(BaseModel):
    version: int = 1
    top: str | None = None
    steps: dict[str, StepRecord] = Field(default_factory=dict)
    gates: dict[str, GateRecord] = Field(default_factory=dict)
    feedback: dict[str, list[str]] = Field(default_factory=dict)  # pending change requests per step
    feedback_history: dict[str, list[str]] = Field(default_factory=dict)
    # questions closed by approving with their default assumption: step -> question id -> default
    accepted_defaults: dict[str, dict[str, str]] = Field(default_factory=dict)
    # every question closed so far (kept after reruns drop it): step -> id -> {question, answer, how, at}
    closed_questions: dict[str, dict[str, dict]] = Field(default_factory=dict)
    # step-private bookkeeping (e.g. spec: hashes of intent/template at last full write, applied answers)
    step_meta: dict[str, dict] = Field(default_factory=dict)
    total_cost_usd: float = 0.0
    # per-section review tracking: step -> section id -> {"generated": sha, "reviewed": sha | None}
    sections: dict[str, dict[str, dict]] = Field(default_factory=dict)
    cost_resets: list[dict] = Field(default_factory=list)  # [{"at": iso, "amount_usd": x}] audit of cleared totals

    @classmethod
    def load(cls, path: Path) -> "PipelineState":
        if path.is_file():
            return cls.model_validate_json(path.read_text())
        return cls()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(self.model_dump_json(indent=2) + "\n")
        tmp.replace(path)

    def step(self, name: str) -> StepRecord:
        return self.steps.setdefault(name, StepRecord())
