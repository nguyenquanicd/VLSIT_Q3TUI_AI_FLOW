"""Test plan of the VLSIT testbench: models the LLM returns, normalisation and REQ coverage (code)."""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, Field

from q3tui.steps.common import Question


class TbCase(BaseModel):
    tc_id: str = Field("", description='Test case id "TC-001", "TC-002", … (keep the ids of unchanged test cases; "" for new ones)')
    name: str = Field(description="lower_snake_case name of the test, e.g. reset_boot_addr")
    description: str = Field("", description="One line: what is stimulated and what is checked")
    req_ids: list[str] = Field(default_factory=list, description="REQ ids this test case checks")
    conditional_param: str | None = Field(None, description="A top-level parameter that must be enabled for this test to apply (PR_… / PARA_…), else null")
    conditional_value: str | None = Field(None, description="The value it must have (null: any non-zero value)")
    source: str = Field("", description="The user's imported test this test case is ported from (its path exactly as listed), else \"\"")


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


def normalize(cases: list[TbCase], known_reqs: set[str], previous: list[dict] | None = None,
              sources: set[str] | None = None) -> tuple[list[TbCase], list[str]]:
    """Ids unique and numbered (new ones continue after the highest), names made file-safe, unknown REQ ids and unknown
    imported test paths (`sources`) dropped. Returns (cases, warnings)."""
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
        source = c.source.strip()
        if source and source not in (sources or set()):
            warnings.append(f"{tid}: '{source}' is not one of the imported tests: ported from nothing")
            source = ""
        out.append(c.model_copy(update={"tc_id": tid, "name": name, "req_ids": ok, "source": source}))
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


def unported(cases: list[dict], tests: list[str]) -> list[str]:
    """Imported tests no test case is ported from."""
    used = {c.get("source") for c in cases}
    return [t for t in tests if t not in used]


def imported_names(tests: list[str]) -> dict[str, str]:
    """Test case name of each imported test: its file name without what every one of them shares
    (`ts.wrap_len2_axi_sram_test.sv` → `wrap_len2`), file-safe."""
    stems = {t: re.sub(r"[^a-z0-9]+", "_", Path(t).name.split(".sv")[0].lower()).strip("_") for t in tests}
    tok = {t: s.split("_") for t, s in stems.items()}
    if len(tok) > 1:
        lists = list(tok.values())
        pre = 0
        while all(len(x) > pre + 1 and x[pre] == lists[0][pre] for x in lists):
            pre += 1
        suf = 0
        while all(len(x) > pre + suf + 1 and x[-1 - suf] == lists[0][-1 - suf] for x in lists):
            suf += 1
        tok = {t: x[pre:len(x) - suf] for t, x in tok.items()}
    out, used = {}, set()
    for t in tests:
        name = base = "_".join(tok[t]) or "test"
        k = 2
        while name in used:
            name, k = f"{base}_{k}", k + 1
        used.add(name)
        out[t] = name
    return out


def seed_imported(tests: list[str], previous: list[dict] | None = None) -> list[dict]:
    """The test plan imported from your tests: one test case per test, in file order, with a fixed id (kept from the
    previous plan when it already had one for that test), name and source; description and REQs come from the LLM."""
    prev = {c.get("source"): c["tc_id"] for c in previous or [] if c.get("source")}
    top = max([tc_number(c["tc_id"]) for c in previous or []] + [0])
    names = imported_names(sorted(tests))
    seed = []
    for t in sorted(tests):
        tid = prev.get(t)
        if not tid:
            top += 1
            tid = f"TC-{top:03d}"
        seed.append({"tc_id": tid, "name": names[t], "description": "", "req_ids": [], "conditional_param": None,
                     "conditional_value": None, "source": t})
    return seed


def apply_imported(cases: list[dict], seed: list[dict]) -> tuple[list[dict], list[str]]:
    """Your tests are the plan: each keeps its id, name and source (the LLM's description / REQs / condition are kept);
    one the LLM left out is put back; the LLM's other test cases follow, renumbered when they clash."""
    warnings: list[str] = []
    by_source: dict[str, dict] = {}
    for c in cases:
        if c.get("source"):
            by_source.setdefault(c["source"], c)
    by_id = {c["tc_id"]: c for c in cases if not c.get("source")}
    for s in seed:  # (an update that lost a test's `source` but kept its id)
        if s["source"] not in by_source and s["tc_id"] in by_id:
            by_source[s["source"]] = by_id.pop(s["tc_id"])
    out = []
    for s in seed:
        c = by_source.get(s["source"])
        if c is None:
            warnings.append(f"{s['tc_id']} {s['name']}: the plan left out {Path(s['source']).name}: kept, mapped to no requirement")
            out.append(dict(s, description=f"ported from {Path(s['source']).name}"))
        else:
            out.append({**c, "tc_id": s["tc_id"], "name": s["name"]})
    taken_ids = {c["tc_id"] for c in out}
    taken_names = {c["name"] for c in out}
    top = max([tc_number(i) for i in taken_ids] + [0])
    for c in cases:
        if c.get("source") and by_source.get(c["source"]) is c:
            continue
        c = dict(c)
        if c["tc_id"] in taken_ids:
            top += 1
            c["tc_id"] = f"TC-{top:03d}"
        if c["name"] in taken_names:
            c["name"] = f"{c['name']}_extra"
        taken_ids.add(c["tc_id"])
        taken_names.add(c["name"])
        top = max(top, tc_number(c["tc_id"]))
        out.append(c)
    return out, warnings
