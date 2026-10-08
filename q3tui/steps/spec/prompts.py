"""Prompts for step 1 (spec). The section structure comes from the spec template."""

from __future__ import annotations

from q3tui.steps.common import Question

WRITER = """\
You are Q3TUI's specification writer: a senior digital design architect who turns a \
statement of intent into a precise, testable hardware block specification.

The specification is the single source of truth for two independent teams: an RTL \
designer and a verification engineer who will never read each other's code. Anything \
ambiguous will be implemented two different ways, so be concrete.

The document structure is fixed by the project's spec template (given in each request): \
produce exactly those sections, in that order, using their ids; fill every template \
field. Section content is Markdown without the section heading (use ### for subsections).

Rules:
- Describe externally observable behaviour; do not choose microarchitecture (FIFO \
implementation, pipeline stages) unless the intent or a constraint requires it.
- When the intent prescribes the design's structure — a module hierarchy, module names, \
pipeline stages, which module does what — copy it exactly into the `structure` section \
(the hierarchy as a tree of module names). It is the user's decision: never drop it, \
rename its modules or reorganise it.
- Never leave a behaviour implicit. When the intent does not decide something, pick the \
most conventional choice, write it into the spec marked "*Assumption:*", and add a \
question with that default_assumption. Mark a question blocking only if no reasonable \
default exists. A field you cannot decide at all is "TBD" plus a question.
- Use "shall" for requirements. Use SystemVerilog-legal snake_case names for the module, \
ports and parameters.
- Optional sections: include them only when they apply to this design."""

REVIEWER = """\
You are Q3TUI's specification reviewer: a verification lead reading a draft hardware \
spec before it goes to independent design and verification teams. Your job is to find \
every place where two competent engineers could implement or check the behaviour \
differently, and fix the spec.

Check: every port has width, direction, clock domain and reset value; every protocol \
states exactly when a transfer occurs and what is stable during stalls; every counter \
states width and overflow behaviour (wrap or saturate); simultaneous events (e.g. \
read+write when full/empty) are defined; reset behaviour of every output is defined; \
latencies are given in cycles; parameters have legal ranges; every template field has a \
value consistent with the rest of the document.

Edit, do not rewrite: return ONLY the sections you change (each complete, same ids), \
not the whole document — unchanged sections are kept as drafted. Also return the full \
list of open questions (keep the ids of questions that are still open; use "" for new \
ones). Resolve issues in the text with a conventional choice marked "*Assumption:*" \
whenever possible. Ask at most 5 questions, only about decisions that change the \
design's observable behaviour and have no conventional default; never re-ask something \
the user already answered. Do not write a narrative review: put everything in the \
structured output (an unchanged spec is `sections: []`). Keep a Design Structure section \
(the module tree the intent prescribes) exactly as it is: it is the user's decision."""

UPDATER = """\
You are Q3TUI's specification editor. The user answered open questions, requested \
changes, changed the intent, and/or changed the spec template (added sections, or changed a section's \
guidance/fields). Apply exactly those to the existing specification: write the added \
sections, revise the sections whose template changed, rewrite only the sections the \
answers/changes affect, keep everything else verbatim (the user may have edited the spec \
by hand), and keep terminology and style consistent. Remove any "*Assumption:*" an \
answer replaces. Do not start a new review of the whole document and do not raise new \
questions; only report a conflict if an answer or change contradicts another part of the \
spec in a way you cannot resolve."""


REPLY_FORMAT = """\
Reply with the complete specification as Markdown and nothing else — no preamble, no \
code fence around it — in exactly this layout:

# <document title>

Top module: `<top_module>`

<!-- section: <section id> -->
## 1. <section title>

| | |
|---|---|
| **<field name>** | <value> |

<section text; ### for subsections>

<!-- section: <next section id> -->
## 2. <next section title>
...

<!-- questions -->
## Open questions

- Q: <question> | assumed: <default assumption> | blocking: no | kind: spec_gap

Rules for the layout: one `<!-- section: id -->` line before every section heading, \
sections in template order with their template ids; the field table only for sections \
that have template fields, listing every field in template order ("TBD" if it cannot be \
decided); a `|` inside a value is written `\\|`. The questions block comes last: one \
`- Q:` line per question (kind: spec_gap or design_choice; append `| id: <id>` to keep \
an existing question's id); leave the block empty if there are none."""


def reformat_prompt(problems: list[str]) -> str:
    return ("Your reply could not be used: " + "; ".join(problems) + ".\n\nReply again with the complete specification, "
            "in exactly the required layout:\n\n" + REPLY_FORMAT)


def _feedback_block(feedback: list[str]) -> str:
    if not feedback:
        return ""
    items = "\n".join(f"- {f}" for f in feedback)
    return f"\nChange requests from the user (must be applied):\n{items}\n"


def _answers_block(questions: list[Question], answers: dict[str, str]) -> str:
    if not answers:
        return ""
    by_id = {q.id: q.question for q in questions}
    items = "\n".join(f"- {qid} ({by_id.get(qid, 'earlier question')}): {a}" for qid, a in answers.items())
    return f"\nUser answers to open questions (authoritative; update the spec text and drop these questions):\n{items}\n"


def _template_block(template: str) -> str:
    return f"\n<spec_template>\n{template}\n</spec_template>\n"


def port_intent(intent: str, documents: str) -> str:
    """The 'intent' of a spec ported from the user's own documents (imported): the documents are the source of truth,
    the template gives the structure."""
    head = (f"{intent.strip()}\n\n" if intent.strip() else "")
    return f"""\
{head}Port the user's own specification documents below into the template (read every listed file fully \
yourself — HTML and PDF included — before writing): every section of the template, \
in its order, with its fields. The documents are the source of truth: carry over every fact they state \
(signal tables, burst types, encodings, parameters and defaults, latencies, error conditions, corner cases), \
reworded into the section it belongs to — do not drop, weaken or invent behaviour. Keep module names and \
port names exactly as the documents give them (the user's RTL and tests use them); parameters follow the \
template's naming (e.g. PR_<NAME>), with the documents' original name noted once in the Parameters table. \
A template section or field the documents do not cover: write what follows from them, else "TBD" plus a \
question. Where the documents contradict each other, write the more specific statement and raise a question.

<user_documents>
{documents.strip()}
</user_documents>"""


def draft_prompt(*, intent: str, template: str, feedback: list[str]) -> str:
    return f"""\
Write the specification for this block.

<intent>
{intent.strip()}
</intent>
{_template_block(template)}{_feedback_block(feedback)}
{REPLY_FORMAT}"""


def revise_prompt(*, intent: str, template: str, spec: str, questions: list[Question], answers: dict[str, str], feedback: list[str]) -> str:
    open_q = "\n".join(f"- {q.id}: {q.question}" for q in questions if q.id not in answers) or "(none)"
    return f"""\
Revise the existing specification. Keep everything that is not affected by the intent \
changes, template changes, answers or change requests below; the user may have edited \
the spec by hand, and those edits must be preserved. If the template gained sections, \
write them; if it lost sections, drop them (move anything essential into the closest \
remaining section).

<intent>
{intent.strip()}
</intent>
{_template_block(template)}
<current_spec>
{spec.strip()}
</current_spec>

Currently open questions:
{open_q}
{_answers_block(questions, answers)}{_feedback_block(feedback)}
{REPLY_FORMAT}"""


def review_prompt(*, intent: str, template: str, draft, rendered: str, answers: dict[str, str]) -> str:
    qs = "\n".join(f"- [{q.id or 'new'}] {q.question} (assumed: {q.default_assumption or '-'})" for q in draft.questions) or "(none)"
    answered = "\n".join(f"- {k}: {v}" for k, v in answers.items()) or "(none)"
    return f"""\
Review and improve this draft specification of `{draft.top_module}`.

<intent>
{intent.strip()}
</intent>
{_template_block(template)}
<draft_spec>
{rendered.strip()}
</draft_spec>

Draft's open questions:
{qs}

Already answered by the user (do not ask again):
{answered}

Return only the sections you changed, the full open-question list and a short summary, as structured output."""


def repair_prompt(*, template: str, draft, errors: list[str]) -> str:
    return f"""\
This specification does not follow the project's spec template. Fix exactly these \
problems and keep all other content unchanged.

Problems:
{chr(10).join('- ' + e for e in errors)}
{_template_block(template)}
Specification (JSON):
{draft.model_dump_json(indent=1)}

Return the corrected specification as structured output."""


def apply_prompt(*, template: str, spec: str, answers: list[tuple[str, str, str]], feedback: list[str],
                 add: list[str] | None = None, revise: list[str] | None = None, intent_change: tuple[str, str] | None = None) -> str:
    answered = "\n".join(f"- {qid}: {question}\n  → answer: {answer}" for qid, question, answer in answers) or "(none)"
    template_work = ""
    if add:
        template_work += "\nSections added to the template — write them (id, then guidance):\n" + "\n".join(f"- {a}" for a in add) + "\n"
    if revise:
        template_work += ("\nSections whose template guidance/fields changed — revise them to match (keep content that "
                          "still fits):\n" + "\n".join(f"- {r}" for r in revise) + "\n")
    if intent_change:
        template_work += (f"\nThe user changed the intent. Revise exactly the sections this change affects; keep the rest.\n"
                          f"<old_intent>\n{intent_change[0].strip()}\n</old_intent>\n<new_intent>\n{intent_change[1].strip()}\n</new_intent>\n")
    return f"""\
Update the specification with these user decisions.

Answers to open questions:
{answered}
{_feedback_block(feedback)}{template_work}{_template_block(template)}
<current_spec>
{spec.strip()}
</current_spec>

Return ONLY the sections that change (complete, with their fields), plus conflicts (usually none) and a short summary, as structured output."""
