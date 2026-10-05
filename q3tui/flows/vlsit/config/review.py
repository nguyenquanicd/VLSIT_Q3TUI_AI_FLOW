"""Your reviews of config parameters (Gate 2) — the API the TUI keys and the assistant tools call (through `Ops`).
Code only, never an LLM stage.

- `pending_ids`: parameters changed from their default that you have not confirmed yet. A parameter kept at its
  default needs no review — nothing about it to check. Reviews are kept in schemas/config_reviews.json (survive a
  reset) and are not an input of the config step: confirming a parameter does not make anything stale, same as
  sva/review.py and parse/review.py.
- Gate 2 is "auto" by default (FLOW.md: it signs itself when its constraints pass) — this only matters when you end
  up pressing Approve yourself (a blocking question stopped the auto-sign, or you switched the gate to human).
"""

from __future__ import annotations

from q3tui.pipeline.engine import EngineError
from q3tui.core.project import read_json, write_json
from q3tui.steps.vlsit import artifacts
from q3tui.steps.vlsit.base import layout


def _params(engine) -> dict[str, dict]:
    data = artifacts.read(engine.project, "final_config.json") or {}
    return data.get("parameters", {}) or {}


def reviews_path(engine):
    return layout(engine).artifact("config_reviews.json")


def reviews(engine) -> dict[str, dict]:
    return read_json(reviews_path(engine), default={}) or {}


def pending_ids(engine) -> list[str]:
    revs = reviews(engine)
    return [name for name, p in _params(engine).items()
            if p.get("changed_from_default") and revs.get(name, {}).get("status") != "confirmed"]


def review_parameter(engine, name: str, confirmed: bool) -> str:
    params = _params(engine)
    if name not in params:
        raise EngineError(f"unknown parameter '{name}' (known: {', '.join(sorted(params)[:12])})")
    revs = reviews(engine)
    revs[name] = {"status": "confirmed" if confirmed else "pending", "at": artifacts.now()}
    write_json(reviews_path(engine), revs)
    msg = f"{name}: {'confirmed' if confirmed else 'back to pending'}"
    engine.bus.emit("log", None, message=msg)
    return msg


def confirm_all(engine, note: str = "") -> str:
    """Confirm every parameter still changed from default and not yet reviewed."""
    revs = reviews(engine)
    ids = pending_ids(engine)
    for name in ids:
        revs[name] = {"status": "confirmed", "note": note, "at": artifacts.now()}
    write_json(reviews_path(engine), revs)
    msg = f"confirmed {len(ids)} parameter(s)"
    engine.bus.emit("log", None, message=msg)
    return msg
