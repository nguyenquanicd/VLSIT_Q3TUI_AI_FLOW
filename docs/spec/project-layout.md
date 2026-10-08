# Project Layout, State and Entry Points

Q3TUI works inside a **project directory**, one per design block. The deliverables
are ordinary visible folders. Internal state lives in `.q3tui/`. The layout below is the
VLSIT flow's ([vlsit-flow.md](vlsit-flow.md) "Layout" has every artifact); other flows
write where their steps say.

```text
<project>/
├── q3tui.yaml              ← optional project config (overrides ~/.q3tui/q3tui.yaml)
├── tools.json              ← optional tools by role (docs/spec/flows.md); default: the built-in Synopsys preset
├── flows/                  ← optional project flows (win over built-in ones by name)
├── prompts/                ← optional system-prompt overrides per step / stage
├── spec/
│   ├── intent.md               ← input to the spec step (optional; `q3tui init` writes a skeleton)
│   ├── template.yaml           ← optional per-project spec template (sections/fields)
│   ├── spec.md                 ← spec step output, or your own spec (any .md/.pdf/... here)
│   └── questions.json, questions.md
├── schemas/                ← JSON artifacts: structured_spec.json, final_config.json, synth_report.json,
│                             selected_testplan.json, rtm.json, verification_report.json; sva_reviews.json and
│                             rtm_signoffs.json (your reviews, kept on reset); questions/<step>.*; NOTES.md (flow session)
├── src/rtl/ src/tb/ src/sva/   ← RTL, testbench, assertions (each with its filelist.f)
├── docs/specification.md (.pdf)
├── sim/vlsit_verify/       ← verify's copies of the simulation logs (stages never read .q3tui/)
└── .q3tui/                 ← internal; never visible to LLM stages; safe to delete (loses history)
    ├── pipeline.json           ← step states, gates, input/output hashes, step_meta, answers, change requests
    ├── cache/                  ← stage checkpoints (cache/stages/<step>/), parsed design
    ├── team/roles.json         ← persistent LLM sessions (flow / team roles)
    ├── prompts/<step>.md       ← the built-in prompt of each stage as last run
    ├── bkp/<timestamp>/        ← copies of files before any agent edit or reset
    ├── stats.jsonl             ← usage per LLM stage and step
    ├── tui/                    ← layout.json, the assistant's session.json
    └── runs/<run_id>/events.jsonl   ← every LLM/tool event
```

Folder names are configurable (`project.*` in config); these are the defaults.

## Step state and staleness

`.q3tui/pipeline.json` records, for each step: status (`pending` / `running` /
`done` / `failed` / `waiting_gate`), the content hashes of its inputs and outputs, the
run id and a timestamp. A step left `running` by a killed process is re-run.

A step is **stale** when any input hash differs from the one recorded when it last ran
(inputs include its options, flow notes and pass conditions, and its `checks_version`).
For example, you edit `spec.md`, so *parse* becomes stale, and then everything after it.
Staleness follows the flow's dependency graph (VLSIT):

```text
spec ─► parse ─► config ─┬─► rtl ─┬─► sva ─► verify ─► doc
                         └─► tb ──┘
```

`q3tui run` runs every step that is pending or stale, in dependency order, and stops
at open gates. Only one process runs a project at a time (a run lock in `.q3tui/`; the
TUI opened while another process runs follows it read-only).

## Entry points: bring your own artifacts

A file you provide counts as the output of its step. That step is marked
`done (user-provided)` and is not regenerated unless you pass `--regenerate <step>`.

| You have | Put it / pass it | Pipeline starts at |
|---|---|---|
| an idea | `--intent "..."`, `--intent-file F` or `spec/intent.md` | spec |
| a spec | `--spec my_spec.pdf` (copied into `spec/`) | parse |
| a project (spec documents, RTL, testbench / tests, SVA) | `q3tui import DIR` / `run --import DIR` | spec: the documents (in `spec/ref/`) are ported into the template's sections; RTL is checked and conformed, tests and SVA are ported ([vlsit-flow.md](vlsit-flow.md) "Importing") |

The assistant's `import_file` does the same (a file or a folder; kind `all | spec | rtl | tb | sva`). What each import
brought in is recorded in `.q3tui/imports/<kind>.json` (source, files and hashes; for RTL the module hierarchy): steps
list it as an input, so a re-import makes them stale.

## Protecting user edits

- Every generated file's hash is recorded. If a file changed since Q3TUI wrote it,
  it was edited by hand. Q3TUI never overwrites a hand-edited file silently: it asks
  first, or with `--yes` it backs the file up to `.q3tui/bkp/` before overwriting.
- `q3tui status` shows hand-edited files and which steps are stale.
- `q3tui reset STEP` (TUI: `R` or `/reset STEP`) deletes a step's *generated*
  outputs, questions/answers and review state, and by default those of every downstream
  step. Inputs you wrote (`intent.md`, `template.yaml`), user-provided artifacts and your
  reviews / sign-offs are never deleted. Everything removed is backed up to
  `.q3tui/bkp/<timestamp>/`. Resetting every step of a `session: flow` flow also restarts
  the flow's session.
- LLM stages cannot read or search `.q3tui/` (hook denial + `.ignore`), so backups
  and run logs never leak into a regenerated result.
