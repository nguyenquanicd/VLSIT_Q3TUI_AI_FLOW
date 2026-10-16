# The Pipeline Engine

The engine (`pipeline/engine.py`) runs a **flow**: the steps of a flow file, by kind, in
dependency order ([flows.md](flows.md)). The default flow is `vlsit`
([vlsit-flow.md](vlsit-flow.md)):

```text
spec ─► parse ─► config ─► rtl ‖ tb ─► sva ─► verify ─► doc
```

Everything below holds for every flow: the engine never hardcodes step names (it asks the
flow: `engine.pipeline_steps`, `engine.label`, `engine.gate_modes()`).

Each step is a **pipeline of stages**. A stage is either deterministic code (D), an LLM
stage (L: an Agent-SDK session with a restricted tool set and structured output), or a
tool run (T). File paths are relative to the project root (see
[project-layout.md](project-layout.md)).

## Changes propagate as updates, not re-runs

**Rule (every step, current and future):** when something upstream changes, each
downstream artifact is *updated* — only the parts the change affects are rewritten, and
everything else stays byte-identical. A step is regenerated from scratch only the first
time, after a reset, or with `--regenerate <step>`.

How every step follows it:

1. **Snapshot.** After each run a step records what it was built from (spec section
   hashes, the REQs, parameters, answers applied) in `pipeline.json` (`step_meta`).
2. **Diff by code.** On the next run the step compares its inputs with the snapshot:
   which sections, REQs, modules, answers, change requests are new or different.
3. **Nothing relevant changed → no LLM.** The step keeps its output as it is (e.g. the
   spec file changed outside its sections). It sets `ctx.unchanged`, reports
   *unchanged*, and **your approval is kept**.
4. **Otherwise a patch.** The LLM gets the current artifact plus exactly what changed,
   and returns only the parts that change (`spec_update`: sections; rtl: the affected
   modules). Code applies it.
5. **Decisions stay decisions.** Answers and accepted defaults of earlier steps are
   passed down as *settled decisions* (`engine.settled_questions`); later steps never ask
   them again (repeats are dropped by code, `drop_settled`). Only to steps allowed to see
   where they came from: the flow file's `sees:` (VLSIT: `rtl` and `tb` get decisions of
   spec, parse and config only). A decision that belongs to an earlier step is handed to
   that step, which updates; the step that raised it follows (see "Questions go to the
   step they belong to").
6. **Review.** An update whose result is unchanged keeps the approval; a real change
   reopens the review of that step (the changed parts are listed in the log).
7. **New checks re-check old results.** A step's `checks_version` is part of its inputs
   hash; bumping it (when Q3TUI's checks on that step's result change) makes its done
   result stale. The re-run changes nothing by itself (no LLM); if the result now fails a
   check it is repaired, and only the repair propagates.

| Step | Update unit | Full regeneration only when |
|---|---|---|
| spec | sections (template edits by code; answers, change requests, **intent changes** → `spec_update`) | first draft, `--regenerate spec` |
| VLSIT steps | parse: REQs of changed sections (stable ids); rtl / sva: modules whose inputs changed; tb: test cases ([vlsit-flow.md](vlsit-flow.md) "Update, not re-run") | first run, reset, `--regenerate` |

## An interrupted step resumes

Every LLM stage's answer is written down the moment it finishes
(`.q3tui/cache/stages/<step>/`, keyed by everything that went into it: stage, prompts,
model, effort, output schema). If the step does not finish — the TUI is closed, the
process is killed, a later stage dies or times out — the next run of that step reuses the
answers of the stages that did finish (no LLM call, no cost; logged as "reusing its result
from the interrupted run") and pays only for the rest. The checkpoints are removed when
the step finishes, when it fails its own checks (`StepFailed`: replaying the same answers
would fail the same way) and on `--regenerate`. Stages with side effects (file writes,
acting tools, resumed sessions) are not checkpointed. A stage that may read files
(`Read` / `Grep` / `Glob`) is keyed on those files too (path, size, mtime of everything it
may read): the same prompt over changed files is asked again, never answered from the
checkpoint. A step left `running` by a killed process is re-run.

## Who sees what

Independence is enforced with `Stage.deny_dirs` / `write_dirs` / `write_files` (hook-level,
not just by prompt), and settled decisions follow the flow's `sees:`. In the VLSIT flow:

| Agent | Sees | Never sees |
|---|---|---|
| spec | intent, spec template, reference documents in `spec/` | — |
| parse, config | spec (+ parse) | — |
| rtl | spec, parse, config | **tb**, **sva** |
| tb | spec, parse, config | **rtl**, **sva** (code compiles the TB against the RTL) |
| sva, verify | the RTL and the artifacts they check | — |

In `session: flow` mode the one session has seen the RTL when it writes the testbench;
the stages still cannot read what they are denied ([vlsit-flow.md](vlsit-flow.md) "One
session"). `.q3tui/` is denied to every stage.

---

## spec — write the specification

Runs only when there is no spec document.

| | |
|---|---|
| Input | `spec/intent.md` (free text, or the skeleton from `q3tui init`) or `q3tui run --intent "..."`; the **spec template** |
| Stages | L draft (sectioned per template; replied as plain Markdown with section markers and a `- Q:` questions block — not JSON, where one bad escape costs a full rewrite; D parses it, one re-ask if the layout is off; file tools only when `spec/` has reference documents) → L self-review (returns only the sections it changes; `spec.self_review`, `--no-spec-review`, `/specreview off`) → D check against the template → L repair (once) → D normalise + render |
| Output | `spec/spec.md` (the specification), `spec/questions.json` / `questions.md` |
| Gate | **spec review**: the user edits `spec.md` or answers the questions, then approves |

**Spec template** defines the document: an ordered list of sections, each with an `id`,
`title`, `required` flag, `guidance` for the writer, and optional **fields**. Fields are
key facts that must be stated explicitly, such as clock frequency, throughput, latency,
target technology, area and power. Lookup order: `<project>/spec/template.yaml` →
`$Q3TUI_HOME/spec_template.yaml` → the built-in template the flow names
(`options: {template: vlsit}` in the VLSIT flow: Overview, Key Features, Parameters,
Interface, Functional Description, Microarchitecture, Timing and Constraints, Registers,
Error Handling, Notes) or the built-in default (Overview, Features, Parameters, Clocks and
Resets, Interfaces, Functional Behaviour, Registers, Corner Cases, Performance,
Implementation Constraints, Verification Notes, …). Users add, remove, reorder and toggle
sections by editing the template, from the TUI's Sections view or with `q3tui init
--template`. The template is an input of the step, so changing it makes the spec stale.

The writer returns structured sections, not free Markdown. Deterministic checks enforce
the template: every required section is present and non-empty, and every required field
has a value. Optional sections are dropped when empty. Extra sections are allowed if
`allow_extra_sections`. Anything still missing after one repair pass becomes `TBD` with
an open question. `spec.md` is rendered with `<!-- section: id -->` markers, so the TUI
can map it back to the template (hand edits keep working).

**Updates are targeted.** Once a spec exists and the intent is unchanged, every update
touches only what changed. Nothing is rewritten wholesale and there is no review pass:

| Change | Handling |
|---|---|
| template: section removed, reordered, renamed | applied to `spec.md` by code, with no LLM (a backup is kept) |
| template: section added, or its guidance/fields/required changed | the LLM writes or revises only those sections |
| answers to questions, change requests | the LLM rewrites only the affected sections |

All of the LLM work above happens in one small stage (`spec_update`). It raises no new
questions, only a *conflict* if an answer contradicts the spec. Answers already applied
are not re-sent. A snapshot of the template is kept at each write so the next run knows
exactly which sections changed. The full draft + self-review (`spec_write` +
`spec_review`, capped at 5 questions) runs only for the first draft or with
`--regenerate spec`. An intent change is an update too: the old and new intent go to
`spec_update`, which revises only the sections the change affects.

The agent describes observable behaviour and does **not** decide microarchitecture here.

## The other steps

The VLSIT steps (parse, config, rtl, tb, sva, verify, doc) are described in
[vlsit-flow.md](vlsit-flow.md); the `example` flow's parse step is the same kind
(`vlsit_parse`).

## The fix loop

A step that dispatched fixes to earlier steps (VLSIT `verify`: triage → scoped change
requests on rtl / tb / sva) sets `step_meta[<step>]["loop"]`. `run` then continues by
itself: the steps with change requests update (only the named units), and the step runs
again — until it passes, a review gate stops it, a blocking question waits for you,
`pipeline.max_fix_iterations` rounds are used (a step may set its own bound, e.g.
`options.max_fix_rounds`), or the same finding came back `pipeline.escalate_after_repeats`
times — then it becomes a question for you.

**Scoped change requests** (`[step:unit] text`, `steps.common.scoped` / `split_feedback`):
a step with units (modules, test cases) re-runs only the units named (`[rtl:<module>]`,
`[sva:<module>]`, `[tb:<tc>]`); an unscoped change request still goes to every unit of the
step.

## Questions and approval

Every step that can ask questions writes the conventional default into its output
(marked *Assumption:*) and lists the question with that `default_assumption`.

- **Answer**: `q3tui answer ID "..."` (TUI: Questions tab). The answer is an input
  of the step, so the step becomes out of date and is updated on the next run.
- **Approve with open questions**: the non-blocking ones are closed as *default
  accepted*. The default is recorded as the answer in `pipeline.json`
  `accepted_defaults`. Nothing is regenerated, because the output already says the same
  thing.
- **Blocking questions** have no safe default, so approval is refused until they are
  answered (`approve --force` overrides; `run --yes` forces with a warning naming them).
- Answering an accepted question differently later works like any answer: the step
  becomes out of date.
- **Steps without a review gate**: their non-blocking questions are closed as *default
  accepted* when the run passes the step (nothing stays open forever). A blocking question
  stops the run with a **one-off review** (the gate dialog, `GateRecord.for_questions`):
  answering the last blocking question ends it and the run continues by itself (the step
  then takes the answer in). A step that only proposes changes to your documents
  (`StepDef.proposes_only`) never has its proposals accepted on your behalf, not even by
  `run --yes` — unless you run unattended.
- **Unattended runs** (`pipeline.auto_answer`, `run --auto-answer`, `/autoanswer on`, the
  settings' General tab; the header shows AUTO-ANSWER): nothing stops for you. Every gate
  approves itself, every question — blocking too — takes its default, and proposals are
  accepted as suggested and the fix loop goes on. Bounded: after
  `pipeline.max_fix_iterations` rounds of accepted proposals, the step waits for you. Use
  it for a first full build or a benchmark; review the documents afterwards.
- **Reviewable items** (`StepDef.pending_reviews()`: VLSIT assertions, flagged
  requirements, parameters) must be confirmed before a human gate approves;
  `pipeline.auto_confirm_reviews` confirms them for you.

## Questions go to the step they belong to

Each artifact has one owner step, so a later step never works around a problem in an
earlier one, and never changes it silently. Every question says where its answer
belongs; the TUI tags it (`→ spec`, `→ parse`, …) and the answer goes there.

| Kind | Meaning | Owner step |
|---|---|---|
| `spec_gap` | the spec is silent, ambiguous or contradictory | `spec` (built in, `GAP_TARGETS`) |
| `req_gap` | a requirement is missing or wrong | the flow's `gap_targets` (VLSIT: `parse`) |
| `arch_gap` | a structural / configuration decision is wrong or makes the task impossible | the flow's `gap_targets` (VLSIT: `config`) |
| `design_choice` | a decision this step makes itself | stays in the asking step |

The answer becomes a `[decision]` change request on the owner step, which updates.

- A gap can only point **back** (a step never hands work to a later one); a gap toward
  the asking step itself or a later step, or toward a step whose output you provided, is
  treated as a design choice (code-checked, `engine.gap_target`). A testbench question
  without a kind is a design choice; any other step's is a spec gap.
- **Answering** a gap question queues the decision on the target step. The next run
  updates the target (targeted, as always) and everything after it follows — including
  the step that asked.
- **Accepting defaults** (approving with open gap questions) hands the defaults over as
  well. When the asking step already contains them, it is marked up to date once the
  target absorbs them instead of re-running (rebase; `StepDef.rebase_problems` can refuse).
  A default that is not in the documents yet is handed to its owner as a change request
  (`StepDef.default_to_apply`).
- **At once, in every mode** (you, `--yes`, auto-approve, auto-answer, a step without a
  review): a decision is a change request on its target the moment it is made, and the run
  goes back to update that step (an LLM patch of only the affected sections) before it
  continues — the documents always say what was decided. Unchanged steps in between are
  skipped; the asking step is rebased, not rebuilt.
- **Blocking** gap questions cannot be approved away with a default: answer them, or
  approve with force.
- **Review kept.** When a run of the target step is caused only by such decisions, its
  review stays approved: the decision was yours. The changed parts show as not reviewed.
- **No silent gap-filling.** Values a repair pass had to invent (e.g. a reset value the
  spec never gave) are raised as spec-gap questions, with the invented value as default.
- **Every question has options** in the picker: the ones its step proposed, else derived
  (`steps/common.py: derive_options`; [flows.md](flows.md)).

## Pipeline controls

```bash
q3tui run [--intent TEXT | --intent-file F] [--spec F ...]
              [--from STEP] [--to STEP] [--only STEP] [--regenerate STEP] [--yes]   # --yes: approve all gates
              [--auto-approve] [--auto-answer] [--spec-review/--no-spec-review]
q3tui status                     # step states, staleness, open gates
q3tui approve STEP|all [--force] # open questions → default accepted; blocking ones must be answered
q3tui change STEP -m "..."       # request a change; the step re-runs with it
q3tui reset STEP|all [--only] [-y]   # delete generated outputs (+ downstream) to start over; backed up first
q3tui questions [--all]          # open questions from all steps
q3tui answer [ID TEXT | all]     # answer one, all interactively, or `all`: every default
q3tui gate STEP MODE             # human | auto | auto_answer | none
q3tui confirm-properties | sign  # VLSIT: confirm assertions, sign requirements off
q3tui flow list | check [FLOW]   # flows; validate one
q3tui tools init | check         # tools by role
q3tui team [status | reset [ROLE]]   # persistent LLM sessions
q3tui stats | cost | config show | design parse|check
q3tui [tui] [--read-only]        # TUI (same engine)
```

`--flow`, `--model`, `--effort`, `--config`, `-C` are global options.
