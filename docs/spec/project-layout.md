# Project Layout, State and Entry Points

Q3TUI works inside a **project directory**, one per design block. Every folder has one role and one owner: **yours**
(you write it, Q3TUI never deletes it), **generated** (a step writes it; `reset` removes it) or **disposable** (tool output; delete
it any time). Internal state lives in `.q3tui/`. A step names no folder: it asks the project (`Project.spec_dir`, `sdc_dir`,
`fpga_dir`, …), and the names are the `project.*` settings ([config.md](config.md)).

```text
<project>/
├── q3tui.yaml              ← optional project config (overrides ~/.q3tui/q3tui.yaml)                        yours
├── .gitignore              ← written by `q3tui init`: build/ and .q3tui/
├── config/                 ← what you configure                                                             yours
│   ├── tools.json              tools by role (docs/spec/flows.md): the one place every flow reads; `q3tui tools init` writes it
│   ├── pdks.json               your standard-cell libraries (the ASIC flow's catalog; same id replaces a built-in)
│   ├── fpgas.json              your FPGA parts (the project flow's FPGA step; the part in use is fpga_synth's option in q3tui.yaml)
│   ├── rtl_rules.md            your RTL rules (rtl flow; `q3tui act rtl_rules_init` writes a skeleton)
│   ├── skills/<name>/SKILL.md  your playbooks (replace or add one the LLM stages may read)
│   └── sanity/<role>_knowledge.md   your CDC / RDC waiver knowledge (cases U1…)
├── flows/                  ← optional project flows (win over built-in ones by name)                        yours
├── prompts/                ← optional system-prompt overrides per step / stage                              yours
├── spec/                   ← the inputs of the spec step                                                    yours
│   ├── intent.md               your idea (optional; `q3tui init` writes a skeleton)
│   ├── template.yaml           optional per-project spec template (sections / fields)
│   ├── spec.md                 the spec step's output, or your own spec (any .md / .pdf / … here)
│   └── ref/                    reference documents imported with `q3tui import` (only the spec step reads them)
├── constraints/
│   └── sdc/                ← sdc.json (source of truth) and the Tcl files rendered from it                  generated, you edit
├── src/
│   ├── rtl/                ← RTL (+ filelist.f); rtl flow: also csr/, style_lint/, smoke/ (below)           generated
│   ├── tb/                 ← the dv flow's UVM testbench (scaffold, integration, tests)      generated
│   ├── fpga/               ← the FPGA flow's own sources: memory initialization files (fpga_port)          generated, you edit
│   └── sva/                ← (reserved: no flow writes assertions now)
├── schemas/                ← JSON artifacts: structured_spec.json, final_config.json, synth_report.json,    generated
│                             fpga_synth / ppa / asic_* / cdc_* / rdc_* …; structured_spec.md;
│                             questions/<step>.{json,answers.json,md} for every step, the spec step included; NOTES.md (flow session)
├── reviews/                ← what you decided: parse_reviews.json, config_reviews.json,                     yours, kept on reset
│                             <role>_answers.json (sanity)
├── reports/                ← readable results rendered by code (sanity <role>_report.md / .csv; the dv flow's tb_architecture.md, dv_review_report.md, dut_conformance_report.md, dv_test_results.md)             generated
├── docs/                   ← the final specification only (specification.md / .pdf; no flow writes it now)  generated
├── build/                  ← tool output, safe to delete                                                    disposable
│   ├── syn/fpga/  syn/asic/    synth.tcl / synth.ys / lec.tcl, reports/*.rpt, netlist/ (.v, .ddc, .svf), sdc/ (the ASIC's own)
│   ├── sim/                    lint / synthesis / simulation logs of the rtl step
│   └── sanity/                 the CDC / RDC setup and waive scripts, the tool's raw reports
└── .q3tui/                 ← internal; never visible to LLM stages; safe to delete (loses history)
    ├── pipeline.json           ← step states, gates, input/output hashes, step_meta, answers, change requests
    ├── cache/                  ← stage checkpoints (cache/stages/<step>/), parsed design
    ├── team/roles.json         ← persistent LLM sessions (flow / team roles)
    ├── prompts/<step>.md       ← the built-in prompt of each stage as last run
    ├── bkp/<timestamp>/        ← copies of files before any agent edit or reset
    ├── fpga/ asic/ sanity/     ← per-step work directories of the tools
    ├── stats.jsonl             ← usage per LLM stage and step
    ├── tui/                    ← layout.json, the assistant's session.json
    ├── tags/<name>/            ← lanes (docs/spec/lanes.md): files/ (the frozen design, read-only), pipeline.json, tag.json,
    │                             results/<lane>.json, changes/<lane>.jsonl (after `q3tui lanes clean`), converged
    ├── lane.json / lane_changes.jsonl   ← in a lane folder only: which tag and lane; what the lane changed (for the merge)
    └── runs/<run_id>/events.jsonl   ← every LLM/tool event

<project>.lanes/<tag>/<lane>/  ← beside the project folder (lanes.dir): one project folder per lane over a tag; spec/ and src/
                                 hard-linked to the tag, copied when a step writes there; its own .q3tui/ and build/       disposable
```

The rtl flow keeps its own working files inside the RTL folder: `src/rtl/csr/` (register map, report, provided modules; `patches/`: the changes made
to the generated modules since the tool made them, applied again when it makes them again),
`src/rtl/style_lint/` (Verible flags, findings) and `src/rtl/smoke/` (the smoke testbench and its report); see
[rtl-flow.md](rtl-flow.md) "Layout". The dv flow ([dv-flow.md](dv-flow.md)) writes `src/tb/` (`project.tb_dir`) and the UVM artifacts
(`golden_manifest.json`, `verification_contract.json`, `tb_architecture.json`, `vip_integration.json`, `dut_integration.json`, `dv_*.json`) and the
`reports/dv_*` / conformance reports. The sva, verify and doc steps were removed with their outputs (`src/sva`, `rtm.json`, `verification_report.json`,
`docs/specification.md`). `reviews/` holds only what is listed above, and `docs/` is reserved for a final specification that no built-in flow writes now.

`build/` is the one place where tool output is meant to be read (by you and by the stages that ask for a report); the
per-step scratch of a run stays in `.q3tui/`. Folder names are configurable (`project.*` in config); these are the defaults.

## Step state and staleness

`.q3tui/pipeline.json` records, for each step: status (`pending` / `running` /
`done` / `failed` / `waiting_gate`), the content hashes of its inputs and outputs, the
run id and a timestamp. A step left `running` by a killed process is re-run.

A step is **stale** when any input hash differs from the one recorded when it last ran
(inputs include its options, flow notes and pass conditions, and its `checks_version`).
For example, you edit `spec.md`, so *parse* becomes stale, and then everything after it.
Staleness follows the flow's dependency graph (spec flow):

```text
spec ─► parse ─► config          (the rtl flow continues: config ─► csr ─► rtl ─► format ─► style_lint ─► smoke)
```

`q3tui run` runs every step that is pending or stale, in dependency order, and stops
at open gates. One process runs a flow at a time (`.q3tui/run.<flow>.lock`; the TUI opened
while another process runs that flow follows it read-only); runs of different flows go on at the
same time (pipeline.md "Runs of several flows").

## Entry points: bring your own artifacts

A file you provide counts as the output of its step. That step is marked
`done (user-provided)` and is not regenerated unless you pass `--regenerate <step>`.

| You have | Put it / pass it | Pipeline starts at |
|---|---|---|
| an idea | `--intent "..."`, `--intent-file F` or `spec/intent.md` | spec |
| a spec | `--spec my_spec.pdf` (copied into `spec/`) | parse |
| a project (spec documents, RTL) | `q3tui import DIR` / `run --import DIR` | spec: the documents (in `spec/ref/`) are ported into the template's sections; RTL is checked and conformed by the rtl flow; testbench, SVA and scripts are left out ([spec-flow.md](spec-flow.md) "Importing") |

The assistant's `import_file` does the same (a file or a folder; kind `all | spec | rtl`). What each import
brought in is recorded in `.q3tui/imports/<kind>.json` (source, files and hashes; for RTL the module hierarchy): steps
list it as an input, so a re-import makes them stale.

## Protecting user edits

- Every generated file's hash is recorded. If a file changed since Q3TUI wrote it,
  it was edited by hand. Q3TUI never overwrites a hand-edited file silently: it asks
  first, or with `--yes` it backs the file up to `.q3tui/bkp/` before overwriting.
- `q3tui status` shows hand-edited files and which steps are stale.
- `q3tui reset STEP` (TUI: `R` or `/reset STEP`) deletes a step's *generated*
  outputs, questions/answers and review state, and by default those of every downstream
  step. Never deleted: what you wrote (`intent.md`, `template.yaml`, `config/`, `flows/`), user-provided
  artifacts, and `reviews/` (your reviews / sign-offs / answers). `build/` holds no state: delete it whenever you like, the
  next run rebuilds it. Everything removed is backed up to
  `.q3tui/bkp/<timestamp>/`. Resetting every step of a `session: flow` flow also restarts
  the flow's session.
- LLM stages cannot read or search `.q3tui/` (hook denial + `.ignore`), so backups
  and run logs never leak into a regenerated result.
