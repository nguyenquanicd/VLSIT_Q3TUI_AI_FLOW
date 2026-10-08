"""Sectioned spec document: LLM output schema, checks against the template, Markdown rendering."""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from q3tui.steps.common import Question
from q3tui.steps.spec.template import SpecTemplate

TBD = "TBD"


class FieldValue(BaseModel):
    name: str = Field(description="Field name exactly as listed in the template")
    value: str = Field(description=f'The value, or "{TBD}" if it cannot be decided (then add a question)')


class SectionOut(BaseModel):
    id: str = Field(description="Template section id (snake_case); new ids only for extra sections")
    title: str
    fields: list[FieldValue] = Field(description="Values for the section's template fields, in template order")
    content: str = Field(description="Section body in Markdown, without the section heading; use ### for subsections")


class SpecDraft(BaseModel):
    top_module: str = Field(description="Proposed top-level SystemVerilog module name (snake_case)")
    title: str
    sections: list[SectionOut]
    questions: list[Question]


class SpecPatch(BaseModel):
    """Targeted update: only the sections that change."""

    sections: list[SectionOut] = Field(description="ONLY the sections whose text changes, each complete (body + fields); unchanged sections are omitted")
    conflicts: list[Question] = Field(description="Only if an answer/change contradicts another part of the spec and you cannot resolve it; usually empty")
    summary: str = Field(description="One or two lines: what changed and why")


class ReviewPatch(BaseModel):
    """Review result: only the sections the reviewer changed, plus the full open-question list."""

    sections: list[SectionOut] = Field(description="ONLY the sections you changed, each complete (body + fields); omit unchanged sections")
    questions: list[Question] = Field(description="The full list of open questions after the review (keep ids of those still open; \"\" for new)")
    summary: str = Field(description="One or two lines: what you fixed")


def apply_review(draft: SpecDraft, patch: ReviewPatch) -> SpecDraft:
    """Replace the reviewed sections in the draft (by id; new ids are appended); take the review's questions."""
    by_id = {s.id: s for s in patch.sections}
    merged = [by_id.pop(s.id, s) for s in draft.sections]
    merged += list(by_id.values())  # sections the reviewer added
    # a review that changed nothing and returned no questions simply left the list out: keep the writer's
    questions = list(patch.questions) if (patch.questions or patch.sections) else list(draft.questions)
    return draft.model_copy(update={"sections": merged, "questions": questions})


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def check(draft: SpecDraft, template: SpecTemplate) -> list[str]:
    """Problems with the draft relative to the template (empty = conforms)."""
    errors: list[str] = []
    by_id = {s.id: s for s in draft.sections}
    ids = [s.id for s in draft.sections]
    dups = {i for i in ids if ids.count(i) > 1}
    if dups:
        errors.append(f"sections appear more than once: {', '.join(sorted(dups))}")
    tmpl = template.by_id()
    for ts in template.sections:
        out = by_id.get(ts.id)
        if out is None:
            if ts.required:
                errors.append(f"required section '{ts.id}' ({ts.title}) is missing")
            continue
        if ts.required and not out.content.strip() and not out.fields:
            errors.append(f"required section '{ts.id}' is empty")
        given = {_norm(f.name) for f in out.fields if f.value.strip()}
        for f in ts.fields:
            if f.required and _norm(f.name) not in given:
                errors.append(f"section '{ts.id}' is missing field '{f.name}' (give a value or {TBD})")
    extras = [i for i in ids if i not in tmpl]
    if extras and not template.allow_extra_sections:
        errors.append(f"sections not in the template: {', '.join(extras)}")
    for i in extras:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", i):
            errors.append(f"extra section id '{i}' must be snake_case")
    return errors


def normalize_section(s: SectionOut, ts) -> SectionOut | None:
    """One section in the template's shape: template title / field names and order, no duplicate heading, extra
    long fields moved into the body. None: an optional section with nothing in it."""
    if not ts.required and not s.content.strip() and all(f.value.strip() in ("", TBD) for f in s.fields):
        return None
    values = {_norm(f.name): f.value.strip() for f in s.fields}
    fields = [FieldValue(name=f.name, value=values.get(_norm(f.name)) or TBD) for f in ts.fields
              if f.required or values.get(_norm(f.name))]
    known = {_norm(f.name) for f in ts.fields}
    content = _strip_heading(s.content, ts.title)
    for f in s.fields:
        if _norm(f.name) in known or not f.value.strip():
            continue
        if "\n" in f.value or len(f.value) > 160:
            # a field the template does not have, holding a table / paragraphs (a model puts "the parameter table" in a field
            # called `table`): it is body text — a field row (`| **table** | \\| a \\| b \\| |`) breaks the Markdown
            label = "" if _norm(f.name) in ("table", "tables", "content", "text", "body") else f"**{f.name}**\n\n"
            content = (content + "\n\n" if content.strip() else "") + label + f.value.strip()
        else:
            fields.append(f)
    return SectionOut(id=ts.id, title=ts.title, fields=fields, content=content)


def placeholder_section(ts) -> SectionOut:
    return SectionOut(id=ts.id, title=ts.title, fields=[], content=f"_{TBD} — not decided yet; see open questions._")


def normalize(draft: SpecDraft, template: SpecTemplate) -> SpecDraft:
    """Template order, template titles/field names, drop empty optional sections, strip duplicate headings;
    fill required sections/fields that are still missing with TBD."""
    tmpl = template.by_id()
    by_id: dict[str, SectionOut] = {}
    for s in draft.sections:
        by_id.setdefault(s.id, s)
    ordered: list[SectionOut] = []
    for ts in template.sections:
        s = by_id.get(ts.id)
        if s is None:
            if not ts.required:
                continue
            s = placeholder_section(ts)
        out = normalize_section(s, ts)
        if out is not None:
            ordered.append(out)
    for s in draft.sections:
        if s.id not in tmpl and template.allow_extra_sections and s.content.strip():
            ordered.append(s.model_copy(update={"content": _strip_heading(s.content, s.title)}))
    return draft.model_copy(update={"sections": ordered})


_CODE_FENCE = re.compile(r"^\s*(?:`{3,}|~{3,})\s*([\w+-]*)")
_CAPTION = re.compile(r"^\*?\s*(Table|Figure)\b", re.I)


def uncaptioned(content: str) -> list[str]:
    """Tables and Mermaid diagrams of a section body with no `Table …` / `Figure …` line directly below them
    (blank lines in between are fine): ["table: | a | b |", "diagram: flowchart LR", ...]."""
    lines = content.splitlines()
    found: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        kind = None
        if _CODE_FENCE.match(line):
            lang = _CODE_FENCE.match(line).group(1).lower()
            j = i + 1
            while j < len(lines) and not (_CODE_FENCE.match(lines[j]) and not _CODE_FENCE.match(lines[j]).group(1)):
                j += 1
            if lang in ("mermaid", "mmd"):
                kind, label = "diagram", (lines[i + 1].strip() if i + 1 < len(lines) else "")
            i = j  # at the closing fence
        elif line.lstrip().startswith("|"):
            j = i
            while j + 1 < len(lines) and lines[j + 1].lstrip().startswith("|"):
                j += 1
            kind, label = "table", line.strip()
            i = j
        if kind:
            k = i + 1
            while k < len(lines) and not lines[k].strip():
                k += 1
            if k >= len(lines) or not _CAPTION.match(lines[k].strip()):
                found.append(f"{kind}: {label[:50]}")
        i += 1
    return found


def _strip_heading(content: str, title: str) -> str:
    lines = content.strip().splitlines()
    if lines and re.match(r"^#{1,3}\s", lines[0]) and _norm(lines[0].lstrip("#")) .endswith(_norm(title)):
        lines = lines[1:]
    return "\n".join(lines).strip()


def section_body(fields: list[FieldValue], content: str) -> str:
    out: list[str] = []
    if fields:
        out += ["| | |", "|---|---|"]
        out += [f"| **{f.name}** | {f.value.replace('|', chr(92) + '|')} |" for f in fields]
        out.append("")
    if content.strip():
        out.append(content.strip())
    return "\n".join(out).strip()


def render(draft: SpecDraft) -> str:
    out = [f"# {draft.title}", "", f"Top module: `{draft.top_module}`", ""]
    for i, s in enumerate(draft.sections, 1):
        out += [f"<!-- section: {s.id} -->", f"## {i}. {s.title}", ""]
        body = section_body(s.fields, s.content)
        if body:
            out += [body, ""]
    return "\n".join(out).rstrip() + "\n"


def tbd_items(sections: list[SectionOut]) -> list[tuple[str, str]]:
    """(section title, field name) of every field still TBD."""
    return [(s.title, f.name) for s in sections for f in s.fields if f.value.strip().upper() == TBD]


def tbd_fields(draft: SpecDraft) -> list[str]:
    return [f"{t}: {n}" for t, n in tbd_items(draft.sections)]


def tbd_questions(sections: list[SectionOut], existing: list[Question]) -> list[Question]:
    """A non-blocking "what should it be?" question for each TBD field no existing question already covers."""
    asked = " ".join(q.question.lower() for q in existing)
    out = []
    for title, name in tbd_items(sections):
        if f"{title}: {name}".lower() in asked or (_word(name, asked) and _word(title, asked)):
            continue
        out.append(Question(id="", question=f"{title}: {name} is TBD — what should it be?", blocking=False, default_assumption=""))
        asked += " " + out[-1].question.lower()
    return out


def _word(word: str, text: str) -> bool:
    return re.search(rf"(?<![a-z0-9_]){re.escape(word.lower())}(?![a-z0-9_])", text) is not None


def missing_required(markdown: str, template: SpecTemplate) -> list:
    """Template sections that are required and absent from a spec.md."""
    have = {sid for sid, _, _ in parse_sections(markdown)}
    return [ts for ts in template.sections if ts.required and ts.id not in have]


def section_out(sid: str, title: str, body: str) -> SectionOut:
    fields, content = _fields_and_content(body)
    return SectionOut(id=sid, title=title, fields=fields, content=content)


def spec_top_module(markdown: str) -> str:
    return next((m.group(1) for ln in markdown.splitlines()[:12] if (m := re.match(r"^Top module:\s*`?([A-Za-z_]\w*)`?", ln.strip()))), "")


_MARK = re.compile(r"^<!-- section: ([a-z][a-z0-9_]*) -->\s*$")


def parse_sections(markdown: str) -> list[tuple[str, str, str]]:
    """(id, title, body) per section of a rendered spec.md (hand edits welcome).

    Uses the `<!-- section: id -->` markers Q3TUI writes; a spec without markers
    (user-written) is split on `## ` headings with ids derived from the titles.
    """
    _, sections = _split(markdown)
    return [(sid, title, "\n".join(body).strip()) for sid, title, body in sections]


def section_id_for(title: str) -> str:
    sid = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_") or "section"
    return sid if sid[0].isalpha() else f"s_{sid}"


_FENCE = re.compile(r"^\s*(```|~~~)")


def _split(markdown: str) -> tuple[list[str], list[list]]:
    """spec.md → (preamble lines, [[id, title, body_lines], ...]). Markers and `## ` lines inside a code fence
    are text (an SVA `## 2` cycle delay at column 0 is not a heading)."""
    lines = markdown.splitlines()
    marked = any(_MARK.match(l) for l in lines)
    preamble: list[str] = []
    sections: list[list] = []
    pending: str | None = None
    fence = False
    for line in lines:
        if _FENCE.match(line):
            fence = not fence
        m = None if fence else _MARK.match(line)
        if m:
            pending = m.group(1)
            continue
        if not fence and line.startswith("## "):
            t = re.sub(r"^\d+(\.\d+)*\.?\s+", "", line[3:].strip())
            s_id = pending if marked and pending else re.sub(r"[^a-z0-9]+", "_", t.lower()).strip("_") or "section"
            sections.append([s_id, t, []])
            pending = None
            continue
        (sections[-1][2] if sections else preamble).append(line)
    return preamble, sections


def _join(preamble: list[str], sections: list[list]) -> str:
    out = list(preamble)
    while out and not out[-1].strip():
        out.pop()
    out.append("")
    for i, (s_id, t, b) in enumerate(sections, 1):
        out.append(f"<!-- section: {s_id} -->")  # invisible; lets Q3TUI map sections from now on
        out.append(f"## {i}. {t}")
        while b and not b[-1].strip():
            b = b[:-1]
        out += b if b and b[0] == "" else ["", *b]
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def upsert_section(markdown: str, sid: str, title: str, body: str, order: list[str] | None = None) -> str:
    """Replace (or insert) one section of a spec.md, keeping everything else verbatim.

    New sections go after the last present section that precedes them in `order`
    (template order); unknown ids go to the end. Headings are renumbered.
    """
    preamble, sections = _split(markdown)
    new_body = body.strip().splitlines()
    for sec in sections:
        if sec[0] == sid:
            sec[1], sec[2] = title or sec[1], ["", *new_body, ""]
            break
    else:
        entry = [sid, title, ["", *new_body, ""]]
        pos = len(sections)
        if order and sid in order:
            rank = {s_id: i for i, s_id in enumerate(order)}
            before = [i for i, sec in enumerate(sections) if rank.get(sec[0], 10**6) < rank[sid]]
            pos = before[-1] + 1 if before else 0
        sections.insert(pos, entry)
    return _join(preamble, sections)


def restructure(markdown: str, order: list[str], titles: dict[str, str], remove: set[str]) -> str:
    """Drop `remove` sections, put the rest in template `order` (unknown ids keep their relative
    place at the end), and use the template's titles. Section bodies are untouched."""
    preamble, sections = _split(markdown)
    kept = [sec for sec in sections if sec[0] not in remove]
    rank = {s_id: i for i, s_id in enumerate(order)}
    kept.sort(key=lambda sec: (rank.get(sec[0], len(order)), 0))
    for sec in kept:
        if sec[0] in titles:
            sec[1] = titles[sec[0]]
    return _join(preamble, kept)


# -- the writer's reply: plain Markdown (no JSON: long documents inside JSON strings break) --

QUESTIONS_MARK = "<!-- questions -->"
_FIELD_ROW = re.compile(r"^\|\s*\*\*(.+?)\*\*\s*\|\s*(.*?)\s*\|\s*$")
_Q_LINE = re.compile(r"^\s*[-*]\s*Q:\s*(.+)$")


def format_questions(questions: list[Question]) -> str:
    """The questions block at the end of the writer's reply (also shown to it as the format)."""
    lines = [QUESTIONS_MARK, "## Open questions", ""]
    for q in questions:
        lines.append(f"- Q: {q.question} | assumed: {q.default_assumption} | blocking: {'yes' if q.blocking else 'no'} | kind: {q.kind}"
                     + (f" | id: {q.id}" if q.id else ""))
    return "\n".join(lines) + "\n"


_Q_ATTR = re.compile(r"\s\|\s(?=(?:assumed|blocking|kind|id)\s*:)", re.I)


def _parse_questions(lines: list[str]) -> list[Question]:
    out = []
    for line in lines:
        m = _Q_LINE.match(line)
        if not m:
            continue
        parts = [p.strip() for p in _Q_ATTR.split(m.group(1))]  # only ` | assumed:` etc. separate: text may contain " | "
        attrs = {}
        for p in parts[1:]:
            key, _, val = p.partition(":")
            attrs[key.strip().lower()] = val.strip()
        kind = attrs.get("kind", "spec_gap")
        out.append(Question(id=attrs.get("id", ""), question=parts[0], blocking=attrs.get("blocking", "no").lower().startswith("y"),
                            default_assumption=attrs.get("assumed", ""), kind=kind if kind in ("spec_gap", "req_gap", "arch_gap", "design_choice") else "spec_gap"))
    return out


def _fields_and_content(body: str) -> tuple[list[FieldValue], str]:
    lines = body.splitlines()
    fields: list[FieldValue] = []
    i = 0
    # a two-column table at the top whose rows are `| **field** | value |`; any header
    # (`| | |`, `| Field | Value |`, `| Constraint | Value |` ...)
    if (len(lines) >= 3 and re.match(r"^\|[^|]*\|[^|]*\|\s*$", lines[0]) and re.match(r"^\|\s*:?-+:?\s*\|\s*:?-+:?\s*\|\s*$", lines[1])
            and _FIELD_ROW.match(lines[2])):
        i = 2
        while i < len(lines) and (m := _FIELD_ROW.match(lines[i])):
            fields.append(FieldValue(name=m.group(1).strip(), value=m.group(2).replace("\\|", "|").strip()))
            i += 1
    return fields, "\n".join(lines[i:]).strip()


def parse_reply(text: str) -> tuple[SpecDraft | None, list[str]]:
    """The writer's Markdown reply → SpecDraft; problems (empty = usable)."""
    lines = text.strip().splitlines()
    # tolerate a preamble or a code fence around the document
    start = next((i for i, ln in enumerate(lines) if ln.startswith("# ")), None)
    if start is None:
        return None, ["no document title line ('# <title>')"]
    lines = [ln for ln in lines[start:] if not re.match(r"^\s*```+\s*(markdown|md)?\s*$", ln)]
    # the questions block starts at the marker; a model that forgot it still writes the `## Open questions` heading
    q_at = next((i for i, ln in enumerate(lines) if ln.strip() == QUESTIONS_MARK), None)
    if q_at is None:
        q_at = next((i for i, ln in enumerate(lines) if re.match(r"^##\s+open questions\s*$", ln.strip(), re.I)), None)
    doc, q_lines = (lines[:q_at], lines[q_at + 1:]) if q_at is not None else (lines, [])
    title = doc[0][2:].strip()
    top = spec_top_module("\n".join(doc[:8]))
    sections = parse_sections("\n".join(doc))
    problems = []
    if not top:
        problems.append("no 'Top module: `<name>`' line under the title")
    if not sections:
        problems.append("no sections (each needs a '<!-- section: <id> -->' line and a '## <n>. <title>' heading)")
    if problems:
        return None, problems
    outs = []
    for sid, stitle, body in sections:
        fields, content = _fields_and_content(body)
        outs.append(SectionOut(id=sid, title=stitle, fields=fields, content=content))
    return SpecDraft(top_module=top, title=title, sections=outs, questions=_parse_questions(q_lines)), []


def render_reply(draft: SpecDraft) -> str:
    """What a well-formed writer reply looks like (used for examples and tests)."""
    return render(draft) + "\n" + format_questions(draft.questions)
