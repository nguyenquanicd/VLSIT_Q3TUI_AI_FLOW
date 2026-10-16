"""Test plan of the VLSIT testbench: models the LLM returns, normalisation and REQ coverage (code)."""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from q3tui.steps.common import Question


class TbCase(BaseModel):
    tc_id: str = Field("", description='Test case id "TC-001", "TC-002", … (keep the ids of unchanged test cases; "" for new ones)')
    name: str = Field(description="lower_snake_case name of the test, e.g. reset_boot_addr")
    description: str = Field("", description="One line: what is stimulated and what is checked")
    req_ids: list[str] = Field(default_factory=list, description="REQ ids this test case checks")
    conditional_param: str | None = Field(None, description="A top-level parameter that must be enabled for this test to apply (PR_… / PARA_…), else null")
    conditional_value: str | None = Field(None, description="The value it must have (null: any non-zero value)")


class TbPlan(BaseModel):
    test_cases: list[TbCase]
    questions: list[Question] = Field(default_factory=list, description=(
        "Questions found while planning (usually none): spec_gap / req_gap (the requirement is wrong or unclear), design_choice"))


class TbUnitResult(BaseModel):
    summary: str = Field("", description="What was written, briefly")
    questions: list[Question] = Field(default_factory=list, description="Questions found while writing (usually none)")


def tc_number(tc_id: str) -> int:
    m = re.search(r"(\d+)$", tc_id)
    return int(m.group(1)) if m else 0


def normalize(cases: list[TbCase], known_reqs: set[str], previous: list[dict] | None = None) -> tuple[list[TbCase], list[str]]:
    """Ids unique and numbered (new ones continue after the highest), names made file-safe, unknown REQ ids dropped.
    Returns (cases, warnings)."""
    warnings: list[str] = []
    prev_by_name = {p["name"]: p["tc_id"] for p in previous or []}
    used: set[str] = set()
    top = max([tc_number(c.tc_id) for c in cases] + [tc_number(p["tc_id"]) for p in previous or []] + [0])
    out = []
    for c in cases:
        name = re.sub(r"[^a-z0-9]+", "_", c.name.lower()).strip("_") or "test"
        tid = c.tc_id if re.fullmatch(r"TC-\d+", c.tc_id or "") else prev_by_name.get(name, "")
        if not tid or tid in used:
            top += 1
            tid = f"TC-{top:03d}"
        used.add(tid)
        ok = [r for r in c.req_ids if r in known_reqs]
        if len(ok) != len(c.req_ids):
            warnings.append(f"{tid}: unknown requirement id(s) dropped: {', '.join(sorted(set(c.req_ids) - set(ok)))}")
        out.append(c.model_copy(update={"tc_id": tid, "name": name, "req_ids": ok}))
    return out, warnings


def applies(c: TbCase | dict, params: dict[str, str]) -> bool:
    """A conditional test case applies when its parameter has the value (any non-zero value when none is named)."""
    param = c["conditional_param"] if isinstance(c, dict) else c.conditional_param
    value = c.get("conditional_value") if isinstance(c, dict) else c.conditional_value
    if not param:
        return True
    have = params.get(param)
    if have is None:
        return True  # an unknown parameter cannot switch a test off
    if value is not None:
        return _norm(have) == _norm(value)
    return _norm(have) not in ("0", "", "false", "1'b0", "0'b0")


def _norm(v: str) -> str:
    v = str(v).strip().lower().replace("_", "")
    m = re.fullmatch(r"\d*'[bdho]?0+", v)
    return "0" if m else v


def file_name(c: TbCase | dict) -> str:
    tid = c["tc_id"] if isinstance(c, dict) else c.tc_id
    name = c["name"] if isinstance(c, dict) else c.name
    return f"tc_{tc_number(tid):03d}_{name}.sv"


def task_name(c: TbCase | dict) -> str:
    tid = c["tc_id"] if isinstance(c, dict) else c.tc_id
    name = c["name"] if isinstance(c, dict) else c.name
    return f"tc_{tc_number(tid):03d}_{name}"


def required_reqs(spec: dict) -> dict[str, str]:
    """Requirements a simulation test case is expected to check (static / assumption requirements are SVA-only)."""
    return {r["req_id"]: r.get("text", "") for r in spec.get("requirements", [])
            if r.get("req_id") and r.get("sva_hint") not in ("static", "assume")}


def coverage(selected: list[TbCase | dict], spec: dict) -> dict:
    need = required_reqs(spec)
    covered = sorted({r for c in selected for r in (c["req_ids"] if isinstance(c, dict) else c.req_ids) if r in need})
    uncovered = sorted(set(need) - set(covered))
    return {"total_req_ids": len(need), "covered_req_ids": covered, "uncovered_req_ids": uncovered,
            "coverage_pct": round(100.0 * len(covered) / len(need), 1) if need else 100.0}
