"""The violation table of a lane: No., Name, Severity, Description, Waived, Comment — `reports/<role>_report.md` (to read) and `.csv`
(for a spreadsheet). Every step of the lane rewrites it from the report and the review's decisions: after the analysis nothing is waived
yet; after the review the Comment says why a violation is waived, or what happens to it (RTL fix, spec question, SDC request, your answer)."""

from __future__ import annotations

import csv
import io
from pathlib import Path

COLUMNS = ("No.", "Name", "Severity", "Description", "Waived", "Comment")


def comment(d: dict | None) -> str:
    if d is None:
        return ""
    sent = d.get("handed_off")
    where = f" — handed off to {sent['flow']}:{sent['step']}" if sent else " — proposed (hand off with sanity_handoff)"
    answer = f" Your answer: {d['answer']}" if d.get("answer") else ""
    match d["action"]:
        case "waive":
            return (f"{d['rule']}: " if d.get("rule") else "") + d["reason"]
        case "fix_rtl":
            return f"RTL fix [{d['module']}]: {d['change']}{where}"
        case "spec":
            return f"Spec question: {d['change']}{where}"
        case "sdc":
            if sent:
                return f"SDC updated (constraints/sdc/extra.tcl): {d.get('sdc_request') or d['reason']}"
            return f"SDC update needed: {d.get('sdc_request') or d['reason']}{answer} — apply it with sanity_handoff, or edit constraints/sdc/sdc.json"
        case _:
            return f"Question for you: {d.get('question') or d['reason']}{answer}"


def rows(report: dict, plan: dict | None) -> list[list[str]]:
    by_id = {d["id"]: d for d in (plan or {}).get("items", [])}
    out = []
    for v in report.get("violations", []):
        d = by_id.get(v["id"])
        out.append([v["id"], v["tag"], str(v.get("severity", "")).title(), v["message"], "Yes" if d and d["action"] == "waive" else "No", comment(d)])
    return out


def _cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def markdown(role: str, report: dict, plan: dict | None) -> str:
    data = rows(report, plan)
    waived = sum(r[4] == "Yes" for r in data)
    lines = [f"# {role.upper()} violations — {report.get('top', '')}", "",
             f"{len(data)} violation(s), {waived} waived, {len(data) - waived} open" + ("" if plan else " (not reviewed yet)"), "",
             "| " + " | ".join(COLUMNS) + " |", "|" + "---|" * len(COLUMNS)]
    lines += ["| " + " | ".join(_cell(c) for c in r) + " |" for r in data]
    return "\n".join(lines) + "\n"


def write(files: list[Path], role: str, report: dict, plan: dict | None) -> None:
    """`files`: [the .md, the .csv] (SanityStep.table_files)."""
    md, csv_file = files
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(markdown(role, report, plan))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(COLUMNS)
    w.writerows(rows(report, plan))
    csv_file.write_text(buf.getvalue())
