# The spec flow (`flows/spec/`)

The first part of the VLSIT RTL flow, ported from `VLSIT_RTL_Generator_AI_Model` (`/autoflow` and its command prompts; the
original orchestrator in `flows/spec/md/autoflow.md`, each step's stage prompts in its `md/skill.md`, Vietnamese — they keep
that wording). Same JSON artifacts (original JSON Schemas in each step's `schemas/`, validated by
`flows/spec/lib/artifacts.py`), same gates, but run by Q3TUI's engine: staleness, update-not-rerun, questions, reset, events,
TUI, assistant. It is Q3TUI's default flow. It ends at the locked configuration; the RTL comes from the [rtl flow](rtl-flow.md),
which reuses these three steps.

```text
spec ─► parse ─► config
 human   Gate 1   Gate 2
         human    auto     (defaults; `pipeline.gate_modes`, `q3tui gate`)
```

No tool role is needed. The steps are LLM stages and code (pyslang for the module map).

The testbench, SVA, verification (simulation, mutation testing, the requirements traceability matrix) and the final document
(`tb`, `sva`, `verify`, `doc`) were part of this flow before; they are removed. Their
artifacts (`selected_testplan.json`, `rtm.json`, `verification_report.json`, `docs/specification.md`) no longer exist.

## Starting point: an intent or a spec

The flow starts at `spec`, which is Q3TUI's spec step with the VLSIT layout (`options: {template: vlsit}`; the original
`/spec_writer` / `/autoflow --describe`):
- **an intent** (`spec/intent.md`, `q3tui run --intent "…"` / `--intent-file FILE`): `spec` writes `spec/spec.md` section by
  section (the 21 sections of the VLSIT ASIC IP Design Specification template: Purpose and Scope … Requirements REQ-<AREA>-<NNN>,
  Features FEAT-NNN, Parameters PARA_* / LPARA_* with constraints C1…, Interfaces, Architecture, Functional Behavior, Register pointer to
  the CSR workbook, RTL contract, Verification, Traceability, Assumptions …), reviews it, and asks what the intent leaves open (questions; the spec gate is human by default).
  What you answer is written into the spec, and `parse` follows the changed sections only.
- **a spec** (`--spec FILE`, or a file in `spec/`): `spec` counts as done (user-provided, never rewritten) and the flow starts at `parse`.
- **a whole project of your own** (`q3tui import DIR`, `q3tui run --import DIR`, the assistant's `import_file` with kind
  `all`): see *Importing your own project* below.
A model that puts a table into an invented field of the template gets it moved into the section body (the Parameters table).

### Importing your own project (Q3TUI extension; VLSIT has no import)

Code (`core/importer.py`, no LLM) sorts a folder by its folder names (`rtl/ src/ hdl/` · `doc(s)/`) and, for HDL files outside
such folders, by their content. Only two kinds are imported: spec documents and RTL. Testbench, test and SVA files (folders
`tb/ sim/ test(s)/ verif/ env/ sva/ assert*/ formal/`; content `bind` / `assert property`, classes, programs, `$finish`,
UVM/VMM/SVT) and the documents of a test environment are recognised only to keep them out of the RTL; scripts, Makefiles,
filelists and images likewise: all are listed as left out. Each kind goes to the step that takes it (`StepDef.import_dir`); a
kind no step of the flow takes is reported, not copied. Re-importing a kind replaces its previous import (backed up). In the
spec flow only spec documents are taken; the `rtl` flow also takes RTL.

| Kind | Goes to | What the flow does with it |
|---|---|---|
| spec documents | `spec/ref/` (name clashes get their folder as prefix) | references: the `spec` step's LLM **ports** them into `spec/spec.md` with the template's predefined sections (Overview, Key Features, Parameters `PR_*`, Interface, …): it reads every document itself (text in the prompt as it is, HTML / PDF with its Read tool — no conversion by code), carries every fact over, keeps module and port names, names parameters by the template (original name noted), and marks what the documents do not cover TBD + a question; the review pass checks it against them. No intent is needed. A re-import updates only the sections the changed documents affect. Only the spec step reads `spec/ref/`; later steps read the ported spec. Documents that were taken as the spec before (identical copies in `spec/`) become references. `--spec FILE` still takes a document as the spec as it is |
| RTL (rtl flow) | the rtl step's own folder | its module names, hierarchy, top and ports (pyslang, at import time: `.q3tui/imports/rtl.json`) are given to `parse` as fact: the module map **is** the imported RTL (`fit_to_design`), a requirement on another module goes to the top. The `rtl` step of the rtl flow then **checks** each imported module (rules, REQ tags, lint) instead of writing it, see [rtl-flow.md](rtl-flow.md) |

The folder imported from, when it lies inside the project (e.g. `ref/<project>/`), is denied to every stage.

## Sessions

`flows/spec/` has `session: fresh`: every stage runs in its own Claude session, its prompt carries everything it needs
(docs/spec/flows.md "Sessions"). `pipeline.session: flow` gives the whole flow one persistent session instead.

## Layout (`project.schemas_dir`)

```text
spec/<ip>_spec.md                       input (or spec step's output); also spec/ref/ (imports), intent.md, template.yaml
schemas/structured_spec.json            parse    Gate 1   (metadata.gate_status, approved_at)
schemas/final_config.json               config   Gate 2   (metadata.gate_2_approved, …)
schemas/questions/<step>.{json,answers.json,md}   questions and answers of every step, the spec step included
reviews/{parse,config}_reviews.json     your per-item reviews (project.reviews_dir; kept across a reset)
.q3tui/…                                internal (backups, run logs)
```

## Steps

| id | kind | LLM stages (read-only unless said) | Code | Gate |
|---|---|---|---|---|
| spec | `spec` (option `template: vlsit`) | as Q3TUI's | | human |
| parse | `vlsit_parse` | `parse_spec`: REQs (`REQ-NNN`, category, `ambiguity_score`, `sva_hint`, feature / parameters affected), parameters, module mapping, ambiguity questions | assemble + validate `structured_spec.json`; `needs_review` when score > 0.3; ambiguous REQs become questions (`spec_gap`; blocking when score ≥ 0.6 after answers cannot settle it), with 2–4 suggested answers each giving its design impact (area / speed / power / complexity), like the spec's; keep / delete / split after 3 rounds; stable ids across updates | 1 human |
| config | `vlsit_config` | `config_overrides` (only when the spec / feedback asks for non-default values) | parameters locked (defaults unless overridden), constraints C1–C5 evaluated by code (`flows/spec/lib/constraints.py`), `final_config.json` | 2 auto (only when all constraints pass) |

### Review API (code)
`flows/spec/parse/review.py` and `flows/spec/config/review.py`: `reviews`, `review_requirement` / `review_parameter`,
`confirm_all` for the requirements flagged for review (Gate 1) and the parameters changed from their default (Gate 2). `Ops`
wraps them for the TUI keys and the assistant tools.

### Update, not re-run
Every step snapshots what it was built from in `step_meta` and, on the next run, re-does only what changed (an unchanged step
sets `ctx.unchanged`). Scoped change requests name the owner step: `[parse] …`, `[config] …`.

## Formats shared with the rtl flow

- `structured_spec.json` (beyond the schema): `metadata.top_module`; per requirement `sva_hint`, `optional`, `needs_human_decision`,
  `rtl_modules`; top-level `modules: [{name, description, req_ids, instances}]`, `module_order` (leaves first).
  A constraint's `rule` in `final_config.json` is the spec's own (the original schema's `const` pin to one design is dropped).
- The base class of these steps (`VlsitStep`), the artifact helpers and the constraint evaluator live in `flows/spec/lib/`
  (`base.py`, `artifacts.py`, `constraints.py`); the rtl flow imports them.

## Regression target
`examples/axi_downscaler/` (spec of `Result/DOWNSCALER_03_09_2026`): same REQ count order of magnitude, same module set,
artifacts validate against the original schemas.

### Answers to config questions are judged by the model

A question the code cannot decide ("Constraint C8 … cannot be evaluated by code. Does it hold?") is answered `yes` (confirmed in code,
no LLM) or in your own words. Your words are read by the `config_overrides` stage, which returns what it overrides / adds as style
rules **and a judgement per constraint**: `confirm` (it holds), `handover` (the answer changes the rule itself: handed to the spec or
requirements step as a decision) or `followup` (a new blocking question with options). Nothing is routed by a fixed question kind.
