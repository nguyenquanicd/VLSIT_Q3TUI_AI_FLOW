# Sanity flow (CDC, RDC)

Runs Synopsys VC Static on the RTL of a project and the **shared SDC**, writes a violation table, lets an LLM review every violation
(waive it, or hand it back), and checks the waivers with the tool again. GCA is not implemented yet.

Design notes: `docs/spec/sanity-flow.md`. This page is the short guide to use it.

## Quick start

```bash
cd projects/<your_project>          # needs src/rtl/ (+ filelist.f) from the vlsit flow
q3tui --flow sanity run             # or press Shift+F in the TUI and pick "Sanity"
```

Or set `pipeline.flow: sanity` in the project's `q3tui.yaml`. The tool comes from `config/tools.json` (`vc_static_shell -f {script}`,
module `synopsys/vc_static`); override it under `tools.roles` in `q3tui.yaml`.

## Steps

| Step | What it does | LLM |
|---|---|---|
| `sdc` | the shared SDC: `constraints/sdc/sdc.json` → `constraints/sdc/*.tcl` (same step as the FPGA / ASIC flows). A hand-written `constraints/sdc/sdc.tcl` without `sdc.json` is used as is | no |
| `cdc` | writes `build/sanity/cdc_setup.tcl`, runs `check_cdc`, reads the report into `schemas/cdc_report.json` + the table | no |
| `cdc_waivers` | reviews each violation against the RTL, SDC, spec and the CDC knowledge; **human gate** | yes (read-only) |
| `cdc_check` | turns the waivers into `waive_cdc` commands (`build/sanity/cdc_waive.tcl`), runs the tool again, compares what is left open | no |
| `rdc`, `rdc_waivers`, `rdc_check` | the same for RDC (`check_rdc`, `waive_rdc`) | as above |

## The violation table

`reports/<cdc|rdc>_report.md` (to read) and `.csv` (spreadsheet). Columns: **No., Name, Severity, Description, Waived, Comment**.
After the analysis nothing is waived; after the review the Comment gives the evidence for a waiver, or says what happens to the violation.
In the TUI: step `cdc_waivers` / `rdc_waivers` → Files tab.

## What the review decides

| Decision | Meaning | Goes to |
|---|---|---|
| `waive` | safe, evidence cited (RTL line, SDC, spec, knowledge rule K1–K5) | a `waive_*` command, applied by `*_check` |
| `fix_rtl` | the RTL is unprotected | a scoped change request for the owner flow's `rtl` step |
| `spec` | the spec owner has to decide | a change request for its `spec` step |
| `sdc` | a constraint is missing or wrong | **you**: a question, the SDC is yours |
| `ask` | something cannot be verified | **you**: a question with suggested answers |

- The CDC knowledge is `cdc_waivers/md/knowledge.md` (K1–K5). Add your own rules in the project: `config/sanity/cdc_knowledge.md`,
  `config/sanity/rdc_knowledge.md`.
- A decision stays while the RTL, SDC and knowledge are unchanged. Only new violations and the ones waiting for an answer go to the LLM again.
- Violations are identified across runs by what they are (tag + objects), not by the tool's per-run number.
- A waiver the tool cannot express (the report names no objects) becomes a question, never an assumption.

## Hand-off and SDC

- After you approve a gate with `fix_rtl` / `spec` decisions, the TUI offers to hand them to the owner flow (option `handoff_flow`,
  default `vlsit`) and switch to it. Without the TUI: `q3tui act sanity_handoff`. Then run the owner flow, come back and run `sanity` again.
- SDC requests are **never** handed off. Answer the question: "I will update the SDC" (change `constraints/sdc/sdc.json`, the SDC tab, then run again) or
  "No SDC change: decide again".

## Settings (`pipeline.step_options.<cdc|rdc>.…` in `q3tui.yaml`, or the Settings dialog)

| Option | Meaning |
|---|---|
| `extra_tcl` | Tcl lines added after the SDC (`configure_cdc_*`, `set_input_delay` for resets, …). Set it on `cdc` / `rdc`; the check step runs the same script |
| `resets` | reset ports, when not taken from `sdc.json` or the RTL |
| `batch` | violations per LLM stage (default 40), on the `*_waivers` step |
| `handoff_flow` | the flow that owns the RTL and spec (default `rtl`) |

The Tcl script is rewritten by code every run (`build/sanity/<role>_setup.tcl`): put your own lines in `extra_tcl`, not in that file.
Each clock of `sdc.json` is put in its own asynchronous group by the script unless the SDC already has clock groups.

## Rules the flow follows

- The tool failing, or not being installed, is a failed step, never a pass. A report that counts violations the reader cannot parse fails the step.
- `*_check` fails when the open violations are not exactly the ones the review did not waive (count and which).
- The RDC report is read with `-include_compressed`: the tool folds similar violations otherwise.

## Layout

```
FLOW.md  tools.json  addon.py           flow, tool commands, hand-off action + settings
sdc/ cdc/ rdc/ *_waivers/ *_check/      one folder per step: step.py, md/skill.md
lib/                                    base, vcstatic (report reader), script, waivers, table, analysis, review, check
```

Tests: `tests/test_sanity.py` (a fake `vc_static_shell` and a fake LLM; real report fixtures in `tests/data/sanity/`).
