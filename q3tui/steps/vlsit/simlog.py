"""Reading simulation output of the VLSIT testbench conventions (docs/spec/vlsit-flow.md "verify").

The testbench prints `TC_RESULT <tc_id> PASS|FAIL|TIMEOUT` per test case (the original `[PASS]` / `[FAIL]` are read too),
SVA violations are the simulator's assertion-failure lines (`Assertion FAILED`, `Error: … a_label`), covers that hit print
`COVER_HIT <label>` (the SVA prompt asks for it in the cover's action block). A test case without a result line is never a
pass: no evidence is a failure ("no result marker").
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

_RESULT = re.compile(r"\bTC_RESULT\s+(\S+)\s+(PASS|FAIL|TIMEOUT)\b", re.I)
_SIMPLE = re.compile(r"\[(PASS|FAIL)\]", re.I)
_VIOLATION = re.compile(r"(assertion\s+(?:failed|violat\w*)|SVA_VIOLATION|\$error\b.*\b[ac]_\w+|error:.*\b[ac]_\w+\b.*(?:fail|violat))", re.I)
_LABEL = re.compile(r"\b([ac]_[A-Za-z0-9_]+)\b")
_DETAIL = re.compile(r"\bTC_DETAIL\s+(\S+)\s+FAIL:\s*(.*)")
_CYCLES = re.compile(r"\bCYCLES\s*[=:]\s*(\d+)")
_COVER = re.compile(r"\bCOVER_HIT\s+([A-Za-z_]\w*)")


@dataclass
class TcOutcome:
    tc_id: str
    status: str  # pass | fail | timeout | skipped
    cycles: int | None = None
    sva_violations: list[str] = field(default_factory=list)  # assertion labels (or the raw line when none is named)
    log_file: str = ""
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def violations(text: str) -> list[str]:
    out: list[str] = []
    for line in text.splitlines():
        if _VIOLATION.search(line):
            m = _LABEL.search(line)
            out.append(m.group(1) if m else line.strip()[:160])
    return out


def cover_hits(text: str) -> set[str]:
    return set(_COVER.findall(text))


def failed_checks(tc_id: str, text: str, limit: int = 5) -> list[str]:
    """The messages of the failed checks (`TC_DETAIL <tc_id> FAIL: <msg>`) of this test case, in order, without repeats."""
    seen: list[str] = []
    for m in _DETAIL.finditer(text):
        if m.group(1) in (tc_id, "") and m.group(2).strip() not in seen:
            seen.append(m.group(2).strip()[:200])
    return seen[:limit]


def parse_run(tc_id: str, text: str, returncode: int | None, timed_out: bool, log_file: str = "") -> TcOutcome:
    """The outcome of one simulation run meant to execute `tc_id`."""
    viol = violations(text)
    cyc = _CYCLES.search(text)
    cycles = int(cyc.group(1)) if cyc else None
    if timed_out:
        return TcOutcome(tc_id, "timeout", cycles, viol, log_file, "the simulation did not finish in time")
    marks = {m.group(1): m.group(2).upper() for m in _RESULT.finditer(text)}
    verdict = marks.get(tc_id)
    if verdict is None and len(marks) == 1 and tc_id not in marks:
        verdict = next(iter(marks.values()))  # a testbench that names its test differently but ran exactly one
    if verdict is None:
        simple = {m.group(1).upper() for m in _SIMPLE.finditer(text)}
        verdict = "FAIL" if "FAIL" in simple else ("PASS" if "PASS" in simple else None)
    if verdict is None:
        return TcOutcome(tc_id, "fail", cycles, viol, log_file, "no result marker (TC_RESULT / [PASS]) in the output"
                         + ("" if returncode in (0, None) else f"; exit code {returncode}"))
    if verdict == "TIMEOUT":
        return TcOutcome(tc_id, "timeout", cycles, viol, log_file, "the testbench reported a timeout")
    if verdict == "FAIL" or viol:
        why = failed_checks(tc_id, text)
        return TcOutcome(tc_id, "fail", cycles, viol, log_file,
                         ("the testbench reported FAIL" + (": " + "; ".join(why) if why else "")) if verdict == "FAIL"
                         else "SVA violation(s) in a passing test")
    return TcOutcome(tc_id, "pass", cycles, [], log_file)
