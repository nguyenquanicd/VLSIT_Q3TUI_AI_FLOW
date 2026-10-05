"""Stage handlers of the VLSIT flow for tests/fakes.FakeLLM: each test module (or implementation) registers a function
`fn(fake, stage, emit) -> StageResult | None` (None: not mine) — `register(fn)` — so the tests of different steps do not
share one growing if-chain."""

from __future__ import annotations

HANDLERS: list = []


def register(fn):
    if fn not in HANDLERS:
        HANDLERS.append(fn)
    return fn


def dispatch(fake, stage, emit):
    for fn in HANDLERS:
        out = fn(fake, stage, emit)
        if out is not None:
            return out
    return None
