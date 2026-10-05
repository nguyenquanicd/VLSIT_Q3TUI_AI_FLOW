"""Usage statistics: tokens per model / step / stage, cost, LLM time and step wall time.

Ledger: `.q3tui/stats.jsonl`, one line per finished LLM stage ("llm") or pipeline
step ("step"). Written by `StatsRecorder` (an event-bus subscriber the engine installs);
built once from the existing run logs (`.q3tui/runs/*/events.jsonl`) when missing.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

TOKEN_KEYS = ("input", "cache_read", "cache_write", "output", "thinking")


def usage_from_result(message: dict) -> dict[str, dict[str, float]]:
    """ResultMessage (as dict) → {model: {input, cache_read, cache_write, output, thinking, cost}}.
    Uses the per-model breakdown (it includes the SDK's own small helper calls)."""
    out: dict[str, dict[str, float]] = {}
    for name, u in (message.get("model_usage") or {}).items():
        model = u.get("canonicalModel") or name
        row = out.setdefault(model, dict.fromkeys((*TOKEN_KEYS, "cost"), 0))
        row["input"] += u.get("inputTokens", 0)
        row["cache_read"] += u.get("cacheReadInputTokens", 0)
        row["cache_write"] += u.get("cacheCreationInputTokens", 0)
        row["output"] += u.get("outputTokens", 0)
        row["thinking"] += u.get("thinkingTokens", 0)
        row["cost"] += u.get("costUSD", 0.0)
    if not out and message.get("usage"):  # older SDKs: totals only
        u = message["usage"]
        out["?"] = {"input": u.get("input_tokens", 0), "cache_read": u.get("cache_read_input_tokens", 0),
                    "cache_write": u.get("cache_creation_input_tokens", 0), "output": u.get("output_tokens", 0),
                    "thinking": (u.get("output_tokens_details") or {}).get("thinking_tokens", 0),
                    "cost": message.get("total_cost_usd") or 0.0}
    return out


def _ts(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


class StatsRecorder:
    """Event-bus subscriber: appends finished LLM stages and steps to the ledger."""

    def __init__(self, path: Path):
        self.path = path
        self._started: dict[str, datetime] = {}

    def _write(self, record: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as fh:
            fh.write(json.dumps(record) + "\n")

    def __call__(self, event) -> None:
        d, ts = event.data, event.ts.isoformat(timespec="milliseconds")
        if event.kind == "llm_done" and d.get("usage") is not None:
            self._write({"type": "llm", "ts": ts, "step": event.step, "stage": d.get("stage"), "model": d.get("model"),
                         "usage": d["usage"], "cost": d.get("cost_usd", 0.0), "duration_ms": d.get("duration_ms", 0),
                         "turns": d.get("turns", 0), "error": bool(d.get("is_error"))})
        elif event.kind == "step_started" and event.step:
            self._started[event.step] = event.ts
        elif event.kind == "step_finished" and event.step in self._started:
            seconds = (event.ts - self._started.pop(event.step)).total_seconds()
            self._write({"type": "step", "ts": ts, "step": event.step, "status": d.get("status"), "seconds": round(seconds, 1)})


def backfill(state_dir: Path) -> int:
    """Build the ledger from the run logs (once, when it does not exist). Returns records written."""
    ledger = state_dir / "stats.jsonl"
    if ledger.exists() or not (state_dir / "runs").is_dir():
        return 0
    records: list[dict] = []
    for events in sorted((state_dir / "runs").glob("*/events.jsonl")):
        started: dict[str, str] = {}
        model: dict[str, str] = {}
        try:
            lines = events.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            kind, step = e.get("kind"), e.get("step")
            if kind == "stage" and e.get("status") == "llm_start":
                model[e.get("name")] = e.get("model")
            elif kind == "sdk" and (e.get("message") or {}).get("type") == "ResultMessage":
                m = e["message"]
                records.append({"type": "llm", "ts": e.get("ts"), "step": step, "stage": e.get("stage"),
                                "model": model.get(e.get("stage")), "usage": usage_from_result(m), "cost": m.get("total_cost_usd") or 0.0,
                                "duration_ms": m.get("duration_ms", 0), "turns": m.get("num_turns", 0), "error": bool(m.get("is_error"))})
            elif kind == "step_started" and step:
                started[step] = e.get("ts")
            elif kind == "step_finished" and step in started:
                a, b = _ts(started.pop(step)), _ts(e.get("ts"))
                if a and b:
                    records.append({"type": "step", "ts": e.get("ts"), "step": step, "status": e.get("status"),
                                    "seconds": round((b - a).total_seconds(), 1)})
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("".join(json.dumps(r) + "\n" for r in records))
    return len(records)


def reset(state_dir: Path) -> str:
    """Start the statistics over: an EMPTY ledger — a missing one is rebuilt from the run logs (`backfill`), so deleting
    the file brought every old run back (apb_slave, 61 runs)."""
    ledger = state_dir / "stats.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("")
    return "statistics reset: counting from now (the run logs are kept)"


def load(state_dir: Path) -> list[dict]:
    backfill(state_dir)
    out = []
    try:
        for line in (state_dir / "stats.jsonl").read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return out


@dataclass
class Row:
    calls: int = 0
    turns: int = 0
    errors: int = 0
    cost: float = 0.0
    llm_s: float = 0.0
    wall_s: float = 0.0
    runs: int = 0  # step runs (wall time)
    tokens: dict[str, float] = field(default_factory=lambda: dict.fromkeys(TOKEN_KEYS, 0))

    @property
    def total_in(self) -> float:
        return self.tokens["input"] + self.tokens["cache_read"] + self.tokens["cache_write"]


@dataclass
class Summary:
    total: Row
    by_model: dict[str, Row]
    by_step: dict[str, Row]
    by_stage: dict[str, Row]
    recent: list[dict]
    first: str | None
    last: str | None


def summarize(records: list[dict], since: str | None = None) -> Summary:
    recs = [r for r in records if not since or (r.get("ts") or "") >= since]
    total, by_model, by_step, by_stage = Row(), defaultdict(Row), defaultdict(Row), defaultdict(Row)
    for r in recs:
        if r.get("type") == "step":
            row = by_step[{"requirements": "req"}.get(r.get("step") or "?", r.get("step") or "?")]
            row.wall_s += r.get("seconds", 0)
            row.runs += 1
            total.wall_s += r.get("seconds", 0)
            continue
        step, stage = r.get("step") or "?", r.get("stage") or "?"
        step = {"requirements": "req"}.get(step, step)  # history from before step 2 was renamed
        for row in (total, by_step[step], by_stage[stage]):
            row.calls += 1
            row.turns += r.get("turns", 0)
            row.errors += r.get("error", False)
            row.cost += r.get("cost", 0.0)
            row.llm_s += r.get("duration_ms", 0) / 1000
        for model, u in (r.get("usage") or {}).items():
            m = by_model[model]
            m.cost += u.get("cost", 0.0)
            for k in TOKEN_KEYS:
                m.tokens[k] += u.get(k, 0)
                for row in (total, by_step[step], by_stage[stage]):
                    row.tokens[k] += u.get(k, 0)
        main = r.get("model")
        if main in by_model:
            by_model[main].calls += 1
            by_model[main].turns += r.get("turns", 0)
            by_model[main].llm_s += r.get("duration_ms", 0) / 1000
    llm = [r for r in recs if r.get("type") == "llm"]
    return Summary(total, dict(by_model), dict(by_step), dict(by_stage), llm[-40:][::-1],
                   recs[0].get("ts") if recs else None, recs[-1].get("ts") if recs else None)


def tokens(n: float) -> str:
    n = int(n)
    return f"{n / 1e6:.2f}M" if n >= 1_000_000 else f"{n / 1e3:.1f}k" if n >= 1000 else str(n)


def duration(s: float) -> str:
    s = int(round(s))
    return f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"


def text_report(summary: Summary, budget: int | None = None) -> str:
    """Plain-text report (CLI and the assistant). `budget`: input tokens per stage call above which a stage is flagged."""
    t = summary.total
    L = [f"{t.calls} LLM stage(s), {t.turns} turns, ${t.cost:.2f}, LLM time {duration(t.llm_s)}, step wall time {duration(t.wall_s)}"
         + (f", {t.errors} failed" if t.errors else ""),
         f"tokens: in {tokens(t.total_in)} (cache read {tokens(t.tokens['cache_read'])}, cache write {tokens(t.tokens['cache_write'])}), "
         f"out {tokens(t.tokens['output'])} (thinking {tokens(t.tokens['thinking'])})", "", "by model:"]
    for model, r in sorted(summary.by_model.items(), key=lambda kv: -kv[1].cost):
        L.append(f"  {model:22} ${r.cost:7.2f}  in {tokens(r.total_in):>7}  out {tokens(r.tokens['output']):>7}  stages {r.calls}")
    L += ["", "by step:"]
    for step, r in summary.by_step.items():
        L.append(f"  {step:12} ${r.cost:7.2f}  in {tokens(r.total_in):>7}  out {tokens(r.tokens['output']):>7}  "
                 f"LLM {duration(r.llm_s):>7}  wall {duration(r.wall_s):>7}  stages {r.calls}")
    heavy = sorted(((s, r) for s, r in summary.by_stage.items() if r.calls), key=lambda kv: -kv[1].total_in / kv[1].calls)[:6]
    if heavy:
        L += ["", "heaviest stages (input tokens per call; each turn re-sends the context):"]
        for stage, r in heavy:
            per = r.total_in / r.calls
            flag = "  ⚠ over llm.stage_token_budget" if budget and per > budget else ""
            L.append(f"  {stage:22} {tokens(per):>7}/call  {r.turns / r.calls:5.1f} turns/call  calls {r.calls}{flag}")
    return "\n".join(L)


def report(state_dir: Path, since: str | None = None) -> Summary:
    return summarize(load(state_dir), since)

