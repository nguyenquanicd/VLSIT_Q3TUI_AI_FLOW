import json
from datetime import datetime, timedelta

from q3tui.core import stats
from q3tui.core.events import Event, EventBus

RESULT = {"type": "ResultMessage", "duration_ms": 42830, "num_turns": 2, "total_cost_usd": 0.04, "is_error": False,
          "usage": {"input_tokens": 10, "output_tokens": 3888},
          "model_usage": {
              "claude-haiku-4-5-20251001": {"inputTokens": 5540, "outputTokens": 13, "costUSD": 0.0056, "canonicalModel": "claude-haiku-4-5"},
              "claude-haiku-4-5": {"inputTokens": 10, "outputTokens": 3888, "cacheCreationInputTokens": 6586, "thinkingTokens": 1260,
                                   "costUSD": 0.0326, "canonicalModel": "claude-haiku-4-5"}}}


def test_usage_merges_models_by_canonical_name():
    u = stats.usage_from_result(RESULT)
    assert list(u) == ["claude-haiku-4-5"]
    h = u["claude-haiku-4-5"]
    assert (h["input"], h["output"], h["cache_write"], h["thinking"]) == (5550, 3901, 6586, 1260)
    assert round(h["cost"], 4) == 0.0382


def test_recorder_and_summary(tmp_path):
    ledger = tmp_path / "stats.jsonl"
    rec = stats.StatsRecorder(ledger)
    t0 = datetime(2026, 9, 26, 20, 0, 0)
    rec(Event("step_started", "spec", {}, t0))
    rec(Event("llm_done", "spec", {"stage": "spec_write", "model": "claude-haiku-4-5", "usage": stats.usage_from_result(RESULT),
                                   "cost_usd": 0.04, "duration_ms": 42830, "turns": 2}, t0 + timedelta(seconds=43)))
    rec(Event("llm_done", "spec", {"stage": "spec_review", "model": "claude-haiku-4-5", "usage": stats.usage_from_result(RESULT),
                                   "cost_usd": 0.04, "duration_ms": 20000, "turns": 2, "is_error": True}, t0 + timedelta(seconds=63)))
    rec(Event("step_finished", "spec", {"status": "done"}, t0 + timedelta(seconds=70)))
    rec(Event("llm_done", "assistant", {"stage": "assistant", "usage": None}, t0))   # no usage: not recorded
    lines = [json.loads(x) for x in ledger.read_text().splitlines()]
    assert [x["type"] for x in lines] == ["llm", "llm", "step"] and lines[2]["seconds"] == 70.0

    s = stats.summarize(stats.load(tmp_path))
    assert s.total.calls == 2 and s.total.errors == 1 and round(s.total.cost, 2) == 0.08 and s.total.wall_s == 70
    assert s.by_step["spec"].calls == 2 and s.by_step["spec"].runs == 1 and round(s.by_step["spec"].llm_s, 1) == 62.8
    assert s.by_model["claude-haiku-4-5"].calls == 2 and s.by_model["claude-haiku-4-5"].tokens["thinking"] == 2520
    assert s.by_stage["spec_write"].tokens["output"] == 3901 and s.recent[0]["stage"] == "spec_review"
    assert stats.summarize(stats.load(tmp_path), since="2026-09-26T20:00:50").total.calls == 1
    report = stats.text_report(s)
    assert "2 LLM stage(s)" in report and "claude-haiku-4-5" in report and "spec" in report


def test_backfill_from_run_logs(tmp_path):
    run = tmp_path / "runs" / "run_1"
    run.mkdir(parents=True)
    events = [
        {"ts": "2026-09-26T20:00:00.000", "kind": "step_started", "step": "arch"},
        {"ts": "2026-09-26T20:00:01.000", "kind": "stage", "step": "arch", "name": "arch_design", "status": "llm_start", "model": "claude-opus-5-5"},
        {"ts": "2026-09-26T20:01:00.000", "kind": "sdk", "step": "arch", "stage": "arch_design", "message": RESULT},
        {"ts": "2026-09-26T20:01:05.000", "kind": "step_finished", "step": "arch", "status": "done"},
    ]
    (run / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\nnot json\n")
    assert stats.backfill(tmp_path) == 2
    assert stats.backfill(tmp_path) == 0           # once
    s = stats.summarize(stats.load(tmp_path))
    assert s.by_step["arch"].wall_s == 65 and s.recent[0]["model"] == "claude-opus-5-5"
    assert stats.backfill(tmp_path / "elsewhere") == 0 and not (tmp_path / "elsewhere").exists()   # no runs: nothing created


def test_engine_records_through_its_bus(tmp_path):
    from q3tui.pipeline.engine import Engine
    from q3tui.core.project import Project

    (tmp_path / "q3tui.yaml").write_text("{}\n")
    bus = EventBus()
    Engine(Project.open(tmp_path), bus)
    Engine(Project.open(tmp_path), bus)            # a second engine on the same bus: still recorded once
    bus.emit("llm_done", "spec", stage="spec_write", model="m", usage={"m": {"input": 1, "output": 2, "cost": 0.1}}, cost_usd=0.1)
    assert len((tmp_path / ".q3tui" / "stats.jsonl").read_text().splitlines()) == 1


def test_a_reset_starts_over_and_old_runs_do_not_come_back(tmp_path):
    """apb_slave: deleting stats.jsonl did not reset anything — a missing ledger is rebuilt from the 61 run logs. A reset
    empties it (CLI `stats --reset`, TUI `R` / `/stats reset`, the assistant's `reset_stats`)."""
    run = tmp_path / "runs" / "run_1"
    run.mkdir(parents=True)
    (run / "events.jsonl").write_text(json.dumps({"ts": "2026-09-26T20:01:00.000", "kind": "sdk", "step": "arch",
                                                  "stage": "arch_design", "message": RESULT}) + "\n")
    assert stats.summarize(stats.load(tmp_path)).total.calls == 1
    assert "statistics reset" in stats.reset(tmp_path)
    assert stats.summarize(stats.load(tmp_path)).total.calls == 0 and (run / "events.jsonl").is_file()
    rec = stats.StatsRecorder(tmp_path / "stats.jsonl")
    rec(Event("llm_done", "spec", {"stage": "spec_write", "model": "claude-haiku-4-5", "usage": stats.usage_from_result(RESULT),
                                   "cost_usd": 0.04, "duration_ms": 1, "turns": 1}, datetime(2026, 9, 30, 21, 0, 0)))
    assert stats.summarize(stats.load(tmp_path)).total.calls == 1                   # counting from now
