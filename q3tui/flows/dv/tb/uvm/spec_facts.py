"""Deterministic facts read from the specification documents (rules DUT-DV-001, 006, 016): the port tables, the places where
the specification itself says it is assuming something, and the design-flow sections that are not behavioral sources.

Only the specification text is read here, never RTL. A fact is a candidate for the LLM / the user to confirm, not a decision."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_ASSUME = re.compile(r"\*?Assumption\*?|\bAs an assumption\b", re.I)
_IDENT = re.compile(r"`?\b([A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+|[A-Z]{2,}[A-Za-z0-9]*|[io]_[a-z0-9_]+|C\d)\b`?")
_GENERIC_HEAD = {"description", "value", "legal", "range", "meaning", "notes", "note", "default", "reset"}
_STOP = set("the a an of to in is are be shall should and or for with that this it as at on by not no from when while after before any all each "
            "only than then its their there which who what also may must can will see per one two both more less into out over under".split())

# sections of a design-flow document that report results derived from the RTL or its testbench: never behavioral sources (DUT-DV-019)
EXCLUDED_HEADINGS = re.compile(r"verification summary|mutation|sign-?off|synthesis report|requirements traceability|simulation results", re.I)


@dataclass
class Port:
    name: str
    direction: str
    width: str
    reset_value: str
    description: str
    section: str
    line: int


@dataclass
class Assumption:
    line: int
    text: str            # the cell or sentence that carries the marker
    subjects: list[str]  # identifiers it is about (the port of a table row, else the identifiers in the sentence)
    topic: list[str]     # what is assumed: the column header of a table cell, else the content words of the sentence
    kind: str            # "cell" | "sentence"
    by_header: bool = False  # cell: the topic is the column header (reset value), else the words of the cell


@dataclass
class Table:
    line: int
    section: str
    header: list[str]
    rows: list[tuple[int, list[str]]] = field(default_factory=list)


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def tables(md: str) -> list[Table]:
    out: list[Table] = []
    section, lines = "", md.splitlines()
    i = 0
    while i < len(lines):
        ln = lines[i]
        m = re.match(r"^#{1,6}\s+(.*)", ln)
        if m:
            section = m.group(1).strip()
        if ln.lstrip().startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[i + 1]):
            t = Table(i + 1, section, [h.strip("* ").strip() for h in _cells(ln)])
            i += 2
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                t.rows.append((i + 1, _cells(lines[i])))
                i += 1
            out.append(t)
            continue
        i += 1
    return out


def _strip(s: str) -> str:
    return re.sub(r"[`*]", "", s).strip()


def ports(md: str) -> list[Port]:
    """Rows of every table whose header has Port and Dir (the interface tables of the specification)."""
    res: list[Port] = []
    for t in tables(md):
        h = [x.lower() for x in t.header]
        if "port" not in h or not any(x.startswith("dir") for x in h):
            continue
        ix = {k: next((j for j, x in enumerate(h) if x.startswith(k)), None) for k in ("port", "dir", "width", "reset", "desc")}
        for line, c in t.rows:
            get = lambda k: _strip(c[ix[k]]) if ix[k] is not None and ix[k] < len(c) else ""  # noqa: E731
            if get("port"):
                res.append(Port(get("port"), get("dir").lower(), get("width"), get("reset"), get("desc"), t.section, line))
    return res


def assumptions(md: str) -> list[Assumption]:
    """Every place the specification marks something as an assumption (a table cell, else a sentence)."""
    found: list[Assumption] = []
    in_table: set[int] = set()
    for t in tables(md):
        for line, c in t.rows:
            in_table.add(line)
            for j, cell in enumerate(c):
                if _ASSUME.search(cell):
                    head = t.header[j] if j < len(t.header) else ""
                    first = _strip(c[0]) if c else ""
                    subj = [first] if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", first) else idents(first)
                    topic = [w for w in words(head) if w not in _GENERIC_HEAD or w == "reset"]
                    text = _strip(cell)
                    found.append(Assumption(line, text, subj, topic or [w for w in words(text) if w != "assumption"], "cell",
                                            bool(topic)))
    for n, ln in enumerate(md.splitlines(), 1):
        if n in in_table or not _ASSUME.search(ln):
            continue
        sent = _strip(re.sub(r"^\s*[-*]\s*", "", ln))
        found.append(Assumption(n, sent, [i for i in idents(sent) if not i.startswith(("PR_", "PARA_"))], words(sent), "sentence"))
    return found


def idents(text: str) -> list[str]:
    return sorted({m.group(1) for m in _IDENT.finditer(text)})


def words(text: str) -> list[str]:
    return sorted({w for w in re.findall(r"[a-z][a-z0-9]{2,}", text.lower()) if w not in _STOP})


def assumed_candidates(req_text: str, notes: list[Assumption]) -> list[Assumption]:
    """Assumptions a requirement may restate as a fact. A table cell matches when the requirement names its row's port and its
    topic (the column: "reset value" -> "reset"); a sentence matches when the requirement shares at least two identifiers or
    words and 40% of the shorter word set with it. Candidates only: the intent stage decides."""
    rw, ri = set(words(req_text)), set(idents(req_text))
    hits = []
    for a in notes:
        topic = set(a.topic)
        if a.kind == "cell":
            need = 1 if a.by_header else 2
            if set(a.subjects) & ri and len(topic & rw) >= need:
                hits.append(a)
        else:
            shared = set(a.subjects) & ri
            tag = re.match(r"\s*(C\d+)\b", req_text)
            if tag and tag.group(1) in a.subjects and re.match(rf"\s*{tag.group(1)}\b", a.text):  # "C2: ..." states constraint C2
                hits.append(a)
            elif shared and len(topic & rw) >= 3 and len(topic & rw) / (min(len(topic), len(rw)) or 1) >= 0.4:
                hits.append(a)
    return hits


def excluded_sections(md: str) -> list[str]:
    """Headings of design-flow result sections in a document (not behavioral sources)."""
    return [m.group(1).strip() for ln in md.splitlines() if (m := re.match(r"^#{1,6}\s+(.*)", ln)) and EXCLUDED_HEADINGS.search(m.group(1))]


def internal_names(req_text: str, known: set[str]) -> list[str]:
    """Identifiers a requirement uses that are neither ports, parameters nor protocol field names: names of the
    implementation's inside (a state, a flag, an instance). Evidence that a requirement may not be observable at the DUT boundary."""
    out = []
    for n in idents(req_text):
        if n in known or n.startswith(("PR_", "PARA_")) or re.fullmatch(r"(?i)(aw|ar|w|r|b)\w*", n) and "_" not in n:
            continue
        if "_" in n and not n.startswith(("i_", "o_")):
            out.append(n)
        elif re.fullmatch(r"S_[A-Z]+|[A-Z]{3,}FIFO", n):
            out.append(n)
    return sorted(set(out))


def original_names(md: str) -> dict[str, str]:
    """Renames the specification lists itself: the parameter table's `Original name` column (name -> original name)."""
    out: dict[str, str] = {}
    for t in tables(md):
        h = [x.lower() for x in t.header]
        if "parameter" in h and "original name" in h:
            a, b = h.index("parameter"), h.index("original name")
            for _, c in t.rows:
                if len(c) > max(a, b) and _strip(c[a]) and _strip(c[b]):
                    out[_strip(c[a])] = _strip(c[b])
    return out
