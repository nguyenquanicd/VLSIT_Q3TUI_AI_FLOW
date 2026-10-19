"""Reader of the VC Static violation reports: `report_violations -app CDC -verbose` (blocks of `Tag : …`, `Description : … [Field] …`,
`Violation : CDC:3` and the fields the check found) and `report_rdc -list` (numbered lines `1.  text`), both under a header
`TAG  (1 error/0 waived)`.

Only fatals, errors and warnings are violations; infos (what the tool inferred) are counted and dropped."""

from __future__ import annotations

import re

_SECTION = re.compile(r"^\s+(\w+)\s+\((\d+) (fatal|error|warning|info)s?/(\d+) waived(?:/\d+ compressed)?\)\s*$")
_FIELD = re.compile(r"^\s+(\w+)\s*:\s*(.*?)\s*$")
_ITEM = re.compile(r"^\s+\d+\.\s+(\S.*?)\s*$")
KEEP = ("fatal", "error", "warning")
_HEADER = re.compile(r"^\s+\w+\s+\((\d+) (fatal|error|warning)s?/")  # (any header that counts errors, whatever follows the waived count)


def declared(text: str) -> int:
    """The fatals, errors and warnings the report's own headers count: what `parse_report` must find (a report it reads as clean is no pass)."""
    return sum(int(m.group(1)) for line in text.splitlines() if (m := _HEADER.match(line)))


def parse_report(text: str) -> tuple[list[dict], int]:
    """(violations, number of infos): each `{tool_id, tag, severity, message, fields}` in report order."""
    blocks: list[dict] = []
    severity, section, cur = "info", "", None
    for line in text.splitlines():
        sec = _SECTION.match(line)
        if sec:
            section, severity, cur = sec.group(1), sec.group(3), None
            continue
        item = _ITEM.match(line)
        if item and section:  # the list format: one numbered line per violation
            blocks.append({"tag": section, "severity": severity, "fields": {"Description": item.group(1)}})
            continue
        f = _FIELD.match(line)
        if f and f.group(1) == "Tag":
            cur = {"tag": f.group(2), "severity": severity, "fields": {}}
            blocks.append(cur)
        elif cur is not None and f and f.group(2) and f.group(1) not in cur["fields"]:
            cur["fields"][f.group(1)] = f.group(2)  # (the first value of a name: nested lists repeat names)
    out, infos = [], 0
    for b in blocks:
        if b["severity"] not in KEEP:
            infos += 1
            continue
        fields = b["fields"]
        desc = fields.pop("Description", b["tag"])
        out.append({"tool_id": fields.pop("Violation", ""), "tag": b["tag"], "severity": b["severity"],
                    "message": re.sub(r"\[(\w+)\]", lambda m: fields.get(m.group(1), m.group(0)), desc), "fields": fields})
        fields.pop("Tag", None)
    return out, infos
