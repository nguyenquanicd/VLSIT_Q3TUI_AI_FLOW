"""Shared pieces for steps: open questions with stable ids, answers files."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from q3tui.core.project import read_json, write_json


# a question about an earlier step's output is handed back to that step ("→ spec"; a flow's `gap_targets` add its own)
GAP_TARGETS = {"spec_gap": "spec"}
KIND_HELP = {
    "spec_gap": "the spec is silent, ambiguous or contradictory about how the DESIGN behaves (the answer is written "
                "into the spec) — never about how this step does its own work",
    "req_gap": "the requirements miss or misstate something (the answer fixes the requirements)",
    "arch_gap": "the architecture's contract (blocks, ports, transactions) is wrong or makes this impossible "
                "(the answer changes the architecture; usually blocking)",
    "design_choice": "a decision this step makes itself; the earlier steps rightly leave it open (the answer stays "
                     "here) — including everything about how this step does its work: how a test is written or rebuilt, "
                     "how long it runs, which tool or technique it uses (rv32im / elastic_buffer: such testbench notes "
                     "were filed as spec gaps and rippled through spec → req → arch → model)",
}


class QuestionOption(BaseModel):
    label: str = Field(description="One possible answer, worded as the answer itself (the text that is recorded)")
    description: str = Field("", description="What choosing it means for the design (one short sentence)")


class Question(BaseModel):
    id: str = Field(description='Keep the id of an unchanged question; use "" for new ones')
    question: str
    blocking: bool = Field(description="True ONLY if no reasonable default exists — you cannot write a "
                                       "default_assumption a competent engineer would accept. A question with a sound "
                                       "default is NOT blocking: the default is used and the user can still change it")
    default_assumption: str = Field(description="What was assumed in the meantime (empty if nothing)")
    kind: Literal["spec_gap", "req_gap", "arch_gap", "design_choice"] = Field(
        "spec_gap",
        description="Where the answer belongs. " + "; ".join(f"{k}: {v}" for k, v in KIND_HELP.items())
        + ". Only use a *_gap kind for a step that comes before the one asking.",
    )

    options: list[QuestionOption] = Field(
        default_factory=list,
        description="ALWAYS propose 2–4 answers (the TUI shows them as a pick list; the user can still type something "
                    "else): the alternatives for a choice, yes / no for a yes-no question, plausible values for an open "
                    "one. The default_assumption should be one of them.")

    @field_validator("kind", mode="before")
    @classmethod
    def _renamed(cls, v):  # question files written before step 2 was renamed "req"
        return "req_gap" if v == "requirements_gap" else v


_YES_NO = re.compile(r"^\W*(does|do|is|are|was|were|should|shall|can|could|will|would|has|have|must|may)\b|\byes or no\b|\banswer yes\b",
                    re.I)
YES_NO_OPTIONS = [{"label": "yes", "description": "confirmed / agreed"},
                  {"label": "no", "description": "not so — use Other… to say what should change"}]
GENERIC_OPTIONS = [{"label": "Decide for me", "description": "use the most common / sensible choice and record it as the decision"},
                   {"label": "Not needed", "description": "leave it unspecified: it does not matter for this design"}]


def derive_options(question: str, default: str = "") -> list[dict]:
    """Suggested answers for a question that came without any (every question gets a pick list): yes / no for a yes-no
    question, else the assumed answer first, then two generic ways out. The user can always type their own."""
    if _YES_NO.search(question or ""):
        return [dict(o) for o in YES_NO_OPTIONS]
    out = [{"label": default.strip(), "description": "what is assumed now"}] if default and default.strip() else []
    return out + [dict(o) for o in GENERIC_OPTIONS]


def assign_question_ids(new: list[Question], previous: list[Question], prefix: str,
                        taken: set[str] = frozenset()) -> list[Question]:
    """Keep ids of questions that already existed (by id or identical text); number new ones. `taken`: ids used before
    (answered and gone from the list) — never given again: their answers are still on file."""
    prev_by_id = {q.id: q for q in previous}
    prev_by_text = {q.question.strip().lower(): q.id for q in previous}
    used = {q.id for q in previous} | set(taken)
    counter = max((int(m.group(1)) for i in used if (m := re.search(r"(\d+)$", i))), default=0)
    out = []
    for q in new:
        qid = q.id if q.id in prev_by_id else prev_by_text.get(q.question.strip().lower(), "")
        if not qid or qid in {o.id for o in out}:
            counter += 1
            qid = f"{prefix}{counter}"
        out.append(q.model_copy(update={"id": qid}))
    return out


def settled_block(settled: list[dict]) -> str:
    """Prompt block: decisions the user already made in earlier steps."""
    if not settled:
        return ""
    items = "\n".join(f"- {q['id']}: {q['question']} → {q['answer']}" for q in settled)
    return ("\nDecisions the user already made in earlier steps (settled: apply them; never ask about "
            f"them again, not even reworded):\n{items}\n")


_STOP = set("a an the is it or and of to on in at for by be are was this that what shall should does do with as if "
             "i e when any from".split())


def _keywords(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9_]+", text.lower()) if w not in _STOP and len(w) > 1}


def _cover(a: str, b: str) -> float:
    """Share of the shorter text's key words that the other text contains (rewordings and
    added explanation don't hide a repeat)."""
    x, y = _keywords(a), _keywords(b)
    return len(x & y) / min(len(x), len(y)) if x and y else 0.0


def drop_settled(questions: list[Question], settled: list[dict]) -> tuple[list[Question], list[tuple[Question, str]]]:
    """Remove questions that repeat a settled one. A repeat: most key words of the shorter
    question are in the other (>= 75%), or many are (>= 60%) and the assumed answer matches.
    Returns (kept, [(dropped, settled id)])."""
    kept, dropped = [], []
    for q in questions:
        hit = None
        for s in settled:
            qc = _cover(q.question, s["question"])
            ac = max(_cover(q.default_assumption, s.get("default_assumption") or ""), _cover(q.default_assumption, s.get("answer") or ""))
            if qc >= 0.75 or (qc >= 0.6 and ac >= 0.6):
                hit = s["id"]
                break
        if hit:
            dropped.append((q, hit))
        else:
            kept.append(q)
    return kept, dropped


def load_answers(path: Path) -> dict[str, str]:
    return read_json(path, default={}) or {}


def save_answer(path: Path, question_id: str, text: str) -> None:
    answers = load_answers(path)
    answers[question_id] = text.strip()
    write_json(path, answers)


def render_questions(questions: list[Question], answers: dict[str, str], title: str, accepted: dict[str, str] | None = None) -> str:
    accepted = accepted or {}
    lines = [f"# {title}", ""]
    open_q = [q for q in questions if q.id not in answers and q.id not in accepted]
    if not open_q:
        lines += ["_No open questions._", ""]
    for q in open_q:
        tag = " **(blocking)**" if q.blocking else ""
        lines += [f"## {q.id}{tag}", "", q.question, ""]
        if q.default_assumption:
            lines += [f"_Assumed for now:_ {q.default_assumption}", ""]
        if q.options:
            lines += ["_Options:_"] + [f"- {o.label}" + (f" — {o.description}" if o.description else "") for o in q.options] + [""]
    closed = [(q, answers[q.id], "answered") for q in questions if q.id in answers]
    closed += [(q, accepted[q.id], "default accepted") for q in questions if q.id not in answers and q.id in accepted]
    if closed:
        lines += ["# Closed", ""]
        lines += [f"- **{q.id}** {q.question} → {a} _({how})_" for q, a, how in closed]
        lines.append("")
    lines.append("_Answer with `q3tui answer <ID> \"...\"` or in the TUI (Questions tab)._")
    return "\n".join(lines) + "\n"


TEXT_SPEC_EXTS = (".md", ".txt", ".rst", ".adoc")


def spec_block(project, docs: list[Path] | None = None, limit: int = 150_000) -> tuple[str, bool]:
    """Spec documents for a prompt: text files inline (the model needs no Read turn), others
    listed to read. Returns (block, whether the stage still needs file tools)."""
    docs = project.spec_documents() if docs is None else docs
    parts, rest, size = [], [], 0
    for p in docs:
        if p.suffix.lower() in TEXT_SPEC_EXTS and size < limit:
            text = p.read_text(errors="replace")
            size += len(text)
            parts.append(f'<spec file="{project.rel(p)}">\n{text.strip()}\n</spec>')
        else:
            rest.append(project.rel(p))
    if rest:
        parts.append("Specification files to read fully (PDFs can be read with the Read tool):\n" + "\n".join("- " + r for r in rest))
    return "\n\n".join(parts) or "(no specification documents)", bool(rest)


_SCOPE = re.compile(r"^\[(\w+):([\w.-]+)\]\s*")


def scoped(step: str, unit: str, text: str) -> str:
    """A change request for one unit of a step (a module, a block, the checker …): `[rtl:fifo] …`. The unit is made a
    single token: one with spaces or brackets (`apb_slave_checker.sv (REQ_001_pready_const)`) was no scope at all and
    reached every test of the testbench (apb_slave: a checker-only fix re-sent all 17 tests to 4 agents)."""
    unit = re.sub(r"[^\w.-]+", "_", unit or "").strip("_") or step
    return f"[{step}:{unit}] {text}"


def split_feedback(feedback: list[str], step: str) -> tuple[dict[str, list[str]], list[str]]:
    """({unit: its change requests}, change requests for the whole step). Only the named units re-run."""
    by_unit: dict[str, list[str]] = {}
    general: list[str] = []
    for f in feedback:
        m = _SCOPE.match(f)
        if m and m.group(1) == step:
            by_unit.setdefault(m.group(2), []).append(f[m.end():])
        else:
            general.append(f)
    return by_unit, general
