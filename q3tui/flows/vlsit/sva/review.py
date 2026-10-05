"""Your reviews — the API the TUI keys and the assistant tools call (through `Ops`). Code only, never an LLM stage.

- `load_properties` / `review_property`: Gate 3b. Only assertions you confirm count for the RTM's condition 2. A rejected one
  becomes a scoped change request to the sva step (`[sva:<module>] …`). Reviews are kept in schemas/sva_reviews.json (survive
  a reset) and are not an input of any step: confirming does not make anything stale.
- `confirm_all`: "approve all" at the gate: every pending, non-vacuous assertion is confirmed (vacuous ones stay pending).
  Approving the gate alone confirms nothing: what you did not confirm simply does not count.
- `rtm_rows` / `sign_req`: Gate 5. You sign a requirement off once conditions 1–5 hold; the LLM never does.
"""

from __future__ import annotations

from q3tui.pipeline.engine import EngineError
from q3tui.core.project import write_json
from q3tui.steps.common import scoped
from q3tui.steps.vlsit import artifacts, rtm

STATUSES = ("pending", "confirmed", "rejected")


def load_properties(engine) -> list[dict]:
    """[{label, module, req_ids, nl, kind: assert|cover, vacuous: bool|None, status, note, file}] (+ line)."""
    return rtm.properties(engine)


def _refresh(engine) -> None:
    from q3tui.flows.vlsit.sva.step import render_review

    render_review(engine)
    rtm.write_rtm(engine)


def review_property(engine, label: str, status: str, note: str = "") -> str:
    if status not in STATUSES:
        raise EngineError(f"status must be one of {', '.join(STATUSES)}")
    all_props = load_properties(engine)
    props = {p["id"]: p for p in all_props}
    if label not in props:  # a bare label works when only one module has it
        same = [p["id"] for p in all_props if p["label"] == label]
        if len(same) > 1:
            raise EngineError(f"'{label}' is in several modules: use one of {', '.join(same)}")
        if not same:
            raise EngineError(f"unknown property '{label}' (known: {', '.join(list(props)[:12]) or 'none'})")
        label = same[0]
    p = props[label]
    if status == "rejected" and not note.strip():
        raise EngineError("say what is wrong with the property (a rejection is sent to the SVA writer)")
    revs = rtm.reviews(engine)
    revs[label] = {"status": status, "note": note.strip(), "at": artifacts.now()}
    write_json(rtm.reviews_path(engine), revs)
    msg = f"{label}: {status}"
    if status == "rejected":
        sva_step = next((s.name for s in engine.steps if s.kind == "vlsit_sva"), "sva")
        engine.request_change(sva_step, scoped(sva_step, p["module"],
                                               f"The reviewer rejected property {label} ({p['nl'] or 'no NL text'}): {note.strip()} "
                                               "Rewrite it (or remove it and cover its requirement another way)."))
        msg += f" — sent back to {sva_step} ({p['module']})"
    _refresh(engine)
    engine.bus.emit("log", None, message=msg)
    return msg


def pending_ids(engine) -> list[str]:
    """Assertions nothing has confirmed or rejected yet — vacuous ones excluded: `confirm_all` never confirms them
    either, so they would never stop being "pending review" for Gate 3b to wait on."""
    return [p["id"] for p in load_properties(engine) if p["status"] == "pending" and p["kind"] == "assert" and not p["vacuous"]]


def confirm_all(engine, include_vacuous: bool = False, note: str = "") -> str:
    """Confirm every pending assertion (vacuous ones only with `include_vacuous`); `note` is recorded with each (the automatic
    confirmation of `options.auto_confirm` says so: it is not the review the original asks for)."""
    revs = rtm.reviews(engine)
    n = skipped = 0
    for p in load_properties(engine):
        if p["status"] != "pending" or p["kind"] != "assert":
            continue
        if p["vacuous"] and not include_vacuous:
            skipped += 1
            continue
        revs[p["id"]] = {"status": "confirmed", "note": note or p["note"], "at": artifacts.now()}
        n += 1
    write_json(rtm.reviews_path(engine), revs)
    _refresh(engine)
    msg = f"confirmed {n} assertion(s)" + (f"; {skipped} vacuous one(s) left pending" if skipped else "")
    engine.bus.emit("log", None, message=msg)
    return msg


def rtm_rows(engine) -> list[dict]:
    return rtm.build_rtm(engine)["requirements"]


def sign_all(engine, note: str = "") -> str:
    """Sign off every requirement whose conditions 1-5 already hold (skips the rest, and ones already signed)."""
    rows = rtm_rows(engine)
    ready = [r["req_id"] for r in rows if not r.get("signed_off") and not rtm.lock_reasons(r)]
    sign = rtm.signoffs(engine)
    for req_id in ready:
        sign[req_id] = {"signed": True, "note": note.strip(), "at": artifacts.now()}
    write_json(rtm.signoffs_path(engine), sign)
    rtm.write_rtm(engine)
    still_locked = sum(1 for r in rows if not r.get("signed_off") and r["req_id"] not in ready)
    msg = f"signed off {len(ready)} requirement(s)" + (f"; {still_locked} still locked (conditions 1-5 not met)" if still_locked else "")
    engine.bus.emit("log", None, message=msg)
    return msg


def sign_req(engine, req_id: str, signed: bool, note: str = "") -> str:
    """Sign a requirement off (or withdraw it). Refused unless conditions 1–5 hold."""
    rows = {r["req_id"]: r for r in rtm_rows(engine)}
    if req_id not in rows:
        raise EngineError(f"unknown requirement '{req_id}'")
    row = rows[req_id]
    sign = rtm.signoffs(engine)
    if signed:
        reasons = rtm.lock_reasons(row)
        if reasons:
            raise EngineError(f"{req_id} cannot be signed off yet: " + "; ".join(reasons))
        sign[req_id] = {"signed": True, "note": note.strip(), "at": artifacts.now()}
        msg = f"{req_id} signed off"
    else:
        sign[req_id] = {"signed": False, "note": note.strip(), "at": artifacts.now()}
        msg = f"{req_id}: sign-off withdrawn"
    write_json(rtm.signoffs_path(engine), sign)
    rtm.write_rtm(engine)
    engine.bus.emit("log", None, message=msg)
    return msg
