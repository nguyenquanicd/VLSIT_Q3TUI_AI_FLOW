"""Your reviews of flagged requirements (Gate 1) — the API the TUI keys and the assistant tools call (through
`Ops`). Code only, never an LLM stage.

- `pending_ids`: requirements Q3TUI flagged (`needs_review`, ambiguity above the review threshold) that you have
  not confirmed as read yet. A requirement with `needs_human_decision` (ambiguity above the *blocking* threshold) is
  already a blocking question handled by `engine.answer` — it is not repeated here.
- `review_requirement` / `confirm_all`: reviews are kept in schemas/parse_reviews.json (survive a reset) and are not
  an input of the parse step: confirming a requirement does not make anything stale, same as sva/review.py.
"""

from __future__ import annotations

from q3tui.pipeline.engine import EngineError
from q3tui.core.project import read_json, write_json
from q3tui.steps.vlsit import artifacts
from q3tui.steps.vlsit.base import layout


def _requirements(engine) -> list[dict]:
    data = artifacts.read(engine.project, "structured_spec.json") or {}
    return data.get("requirements", []) or []


def reviews_path(engine):
    return layout(engine).artifact("parse_reviews.json")


def reviews(engine) -> dict[str, dict]:
    return read_json(reviews_path(engine), default={}) or {}


def pending_ids(engine) -> list[str]:
    revs = reviews(engine)
    return [r["req_id"] for r in _requirements(engine)
            if r.get("needs_review") and not r.get("needs_human_decision") and revs.get(r["req_id"], {}).get("status") != "confirmed"]


def review_requirement(engine, req_id: str, confirmed: bool) -> str:
    ids = {r["req_id"] for r in _requirements(engine)}
    if req_id not in ids:
        raise EngineError(f"unknown requirement '{req_id}' (known: {', '.join(sorted(ids)[:12])})")
    revs = reviews(engine)
    revs[req_id] = {"status": "confirmed" if confirmed else "pending", "at": artifacts.now()}
    write_json(reviews_path(engine), revs)
    msg = f"{req_id}: {'confirmed' if confirmed else 'back to pending'}"
    engine.bus.emit("log", None, message=msg)
    return msg


def confirm_all(engine, note: str = "") -> str:
    """Confirm every requirement still flagged for review (not the blocking ones: those need an actual answer)."""
    revs = reviews(engine)
    ids = pending_ids(engine)
    for rid in ids:
        revs[rid] = {"status": "confirmed", "note": note, "at": artifacts.now()}
    write_json(reviews_path(engine), revs)
    msg = f"confirmed {len(ids)} requirement(s)"
    engine.bus.emit("log", None, message=msg)
    return msg
