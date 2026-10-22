"""The waiver review's data: the decision of each violation, the VC Static waiver commands, the knowledge the review applies, and how a
fix is handed to the flow that owns it.

VC Static numbers its violations per run (`CDC:48` is another violation the next time), so a violation is identified by what it is
(`signature`: the tag and the objects, or the message) and a waiver names it that way."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

ACTIONS = ("waive", "fix_rtl", "spec", "sdc", "ask")
OPEN_ASKING = ("sdc", "ask")  # the decisions that wait for you


class Decision(BaseModel):
    id: str = Field(description="The violation id from the report (V1, V2, …)")
    action: Literal["waive", "fix_rtl", "spec", "sdc", "ask"] = Field(
        description="waive: safe, under a knowledge rule whose conditions you verified, or by a design reason with its evidence; "
                    "fix_rtl: change one RTL module; spec: the spec owner has to decide; "
                    "sdc: a constraint is missing or wrong in the SDC (a plain SDC command in sdc_request is added to constraints/sdc/extra.tcl on hand-off; the user decides otherwise); "
                    "ask: you cannot verify a condition and nothing else settles it: ask the user")
    module: str = Field("", description="action fix_rtl: the module (file name without extension); empty otherwise")
    reason: str = Field(description="waive: why it is safe, citing the evidence (file:line, signal) for each condition; "
                                    "fix_rtl / spec / sdc: what is wrong or undecided; ask: what you checked and what is missing")
    change: str = Field("", description="fix_rtl: the structural change, concretely; spec: the question for the spec; empty otherwise")
    rule: str = Field("", description="waive: the knowledge case it relies on (K1, U2, …); empty for a plain design reason")
    sdc_request: str = Field("", description="action sdc: the constraint to add or change, as one Tcl command with the real object names "
                                             "(e.g. set_max_delay 15.0 -from {…} -to {…}), and where; empty otherwise")
    question: str = Field("", description="action ask / sdc: ONE clear question for the user; empty for the others")
    options: list[str] = Field(default_factory=list, description="action ask: two to four suggested answers; empty otherwise")


class Review(BaseModel):
    decisions: list[Decision] = Field(description="One decision for every violation id of the report, none missing, none invented")
    summary: str = Field("", description="Two or three sentences: what was waived, what needs RTL, spec, SDC work or the user")


def _glob(name: str) -> str:
    return name.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")  # (waive_cdc patterns are globs: `[7:0]` is not a range)


def signature(v: dict) -> tuple:
    f = v.get("fields") or {}
    return (v["tag"], f.get("SrcObject") or f.get("ConvergencePoint") or v.get("message", ""), f.get("DestObject") or "")


def sig_id(role: str, v: dict) -> str:
    """A stable id of a violation across runs (the `V` numbers are not): the id of its question."""
    return f"{role.upper()}-" + hashlib.sha256("\0".join(signature(v)).encode()).hexdigest()[:6]


def _comment(d: dict) -> str:
    why = " ".join(f"{d.get('rule') or 'design'}: {d.get('reason', '')}".split())[:300]
    return why.replace("{", "(").replace("}", ")").replace("\\", "/")


def waive_command(v: dict, d: dict, role: str) -> str | None:
    """The `waive_cdc` / `waive_rdc` of one violation the review waived; None when the report does not say which objects it is about."""
    f = v.get("fields") or {}
    if role == "rdc":  # `report_rdc -list` says nothing else than the tag and the message
        msg = v.get("message", "").replace("{", "*").replace("}", "*")
        return f"waive_rdc -add W_{d['id']} -tag {v['tag']} -msg {{{_glob(msg)}}} -comment {{{_comment(d)}}}" if msg else None
    if v["tag"].startswith("CDC_COHERENCY") and f.get("ConvergencePoint"):
        sel = f'-filter {{ConvergencePoint == "{f["ConvergencePoint"]}"}}'
    elif f.get("SrcObject") and f.get("DestObject"):
        sel = f"-from {{{_glob(f['SrcObject'])}}} -to {{{_glob(f['DestObject'])}}}"
    else:
        return None
    return f"waive_cdc -add W_{d['id']} -tag {v['tag']} {sel} -comment {{{_comment(d)}}}"


def waive_tcl(violations: list[dict], items: list[dict], rtl: str, role: str) -> tuple[str, list[str]]:
    """(the script, the ids it cannot express). Only `waive` decisions: a fix, a constraint or a question stays an error."""
    by_id = {v["id"]: v for v in violations}
    lines, skipped = [f"# {role.upper()} waivers of the review (Q3TUI {role}_waivers), RTL {rtl}: sourced after check_{role}"], []
    for d in items:
        if d["action"] != "waive":
            continue
        cmd = waive_command(by_id[d["id"]], d, role) if d["id"] in by_id else None
        lines.append(cmd) if cmd else skipped.append(d["id"])
    return "\n".join(lines) + "\n", skipped


def knowledge(builtin: Path, config_dir: Path, role: str) -> str:
    """The cases the review applies: the flow's (`<step>/md/knowledge.md`, K1…) then the project's (`config/sanity/<role>_knowledge.md`, U1…: they win)."""
    parts = []
    for label, f in (("Built-in knowledge", builtin), ("Project knowledge (wins over the built-in cases)", config_dir / "sanity" / f"{role}_knowledge.md")):
        if f.is_file() and f.read_text(errors="replace").strip():
            parts.append(f"## {label}\n\n{f.read_text(errors='replace').strip()}")
    return "\n\n".join(parts) or f"(no {role.upper()} knowledge: waive only what you can fully justify from the RTL, the SDC and the spec; ask otherwise)"


def handoff_text(role: str, d: dict, v: dict) -> tuple[str, str]:
    """(step of the owning flow, change-request text) for one `fix_rtl` / `spec` decision."""
    what = f"{role.upper()} {v['tag']}: {v['message'][:160]}"
    if d["action"] == "fix_rtl":
        return "rtl", (f"[rtl:{d['module']}] {what} — {d['change']}. Evidence: {d['reason']} "
                       "Keep the behaviour, the ports and every requirement tag unchanged; lint stays clean.")
    return "spec", (f"{what} — the spec must say: {d['change']}. Context: {d['reason']} Change only the requirements this affects (the RTL is updated "
                    "from those requirements module by module: do not reword or restructure the rest of the spec).")
