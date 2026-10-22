"""`schemas/dv_evidence.json`: what was really compiled and run (rules DUT-DV-011, 012, 022, 023).

An entry exists only for a command that ran. Status is `PASS` only when the tool returned 0 and, for a simulation, the log shows zero
UVM_ERROR and zero UVM_FATAL, the result line says PASS and no DV_TODO was announced. A planning artifact never creates an entry."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

from q3tui.core.project import read_json, write_json

UVM_COUNT = {"UVM_ERROR": re.compile(r"^UVM_ERROR\s*:\s*(\d+)", re.M), "UVM_FATAL": re.compile(r"^UVM_FATAL\s*:\s*(\d+)", re.M)}
RESULT = re.compile(r"VLSIT_DUT_TB_RESULT\s+test=(\S+)\s+tc=(\S*)\s+status=(PASS|FAIL|INCOMPLETE)\s+checks=(\d+)\s+mismatches=(\d+)\s+todo=(\d+)")


def uvm_counts(text: str) -> dict:
    """UVM_ERROR / UVM_FATAL counts from the report summary of a log; None when the summary is not in the log."""
    return {k: (int(m.group(1)) if (m := rx.search(text)) else None) for k, rx in UVM_COUNT.items()}


def judge_run(text: str, returncode: int | None, test: str) -> tuple[str, str]:
    """(status, note) of one simulation log: PASS needs rc 0, zero UVM_ERROR/UVM_FATAL, a PASS result line for this test (DUT-DV-023)."""
    if returncode != 0:
        return "FAIL", f"simulator exit code {returncode}"
    n = uvm_counts(text)
    if n["UVM_ERROR"] is None or n["UVM_FATAL"] is None:
        return "FAIL", "UVM report summary not found in the log"
    if n["UVM_ERROR"] or n["UVM_FATAL"]:
        return "FAIL", f"UVM_ERROR={n['UVM_ERROR']} UVM_FATAL={n['UVM_FATAL']}"
    m = next((x for x in RESULT.finditer(text) if x.group(1) == test or test in ("", x.group(1))), None)
    if m is None:
        return "FAIL", "no VLSIT_DUT_TB_RESULT line for this test"
    if m.group(3) != "PASS":
        return "FAIL", f"result line says {m.group(3)} (todo={m.group(6)}, checks={m.group(4)}, mismatches={m.group(5)})"
    return "PASS", ""


def path(project) -> Path:
    return project.schemas_dir / "dv_evidence.json"


def record(project, kind: str, name: str, res, note: str, at: str, status: str | None = None, seed: int | None = None) -> dict:
    """Append the entry of a command that ran (`res` is a q3tui.eda.base.ToolResult). `status` overrides ok/FAIL for runs judged by the log."""
    data = read_json(path(project), default=None) or {"schema_version": 1, "entries": []}
    log = Path(res.log_path) if res.log_path else None
    text = log.read_text(errors="replace") if log and log.is_file() else ""
    counts = uvm_counts(text) if kind == "run" else {"UVM_ERROR": None, "UVM_FATAL": None}
    entry = {"id": f"{kind}-{len(data['entries']) + 1:03d}", "kind": kind, "name": name, "command": shlex.join(res.command), "returncode": res.returncode,
             "duration_s": round(res.duration_s, 1), "log": project.rel(log) if log else "", "uvm_error": counts["UVM_ERROR"], "uvm_fatal": counts["UVM_FATAL"],
             "seed": seed, "status": status or ("PASS" if res.ok else "FAIL"), "note": note, "at": at}
    data["entries"].append(entry)
    write_json(path(project), data)
    return entry
