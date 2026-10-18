# The Sanity flow (`sanity`) — CDC and RDC analysis with a waiver review

A built-in folder flow (`q3tui/flows/sanity/`, `pipeline.flow: sanity` or `q3tui --flow sanity`) that takes the RTL the VLSIT flow produced
(`src/rtl/`, read only) and the SDC the FPGA and ASIC flows share (`sdc/`), runs static clock-domain and reset-domain crossing analysis with
Synopsys VC Static, lists every violation in a table, and sorts each one into a waiver, an RTL fix, a spec question, an SDC request or a question
for you. Built like the ASIC flow: code runs the tool and reads its report, one read-only LLM stage reviews, the hand-off goes to the flow that
owns the RTL as change requests. The vlsit flow is untouched. GCA is not in this flow (no tool on this site yet: it would be a third lane with its
own role).

```
sdc ─► cdc ─► cdc_waivers [gate: human] ─► cdc_check          (the two lanes are independent)
   └─► rdc ─► rdc_waivers [gate: human] ─► rdc_check
```

| Step | Kind | Does | Writes |
|---|---|---|---|
| `sdc` | `sanity.sdc` | the SDC both flows share (below) | `sdc/*.tcl` (or nothing: yours) |
| `cdc` / `rdc` | `sanity.cdc` / `sanity.rdc` | tool role `cdc` / `rdc` (`vc_static_shell -f`) runs the script Q3TUI writes; every fatal, error, warning is a violation | `sanity/<role>_setup.tcl`, `schemas/<role>_report.json`, the table `sanity/<role>_report.md` / `.csv` |
| `cdc_waivers` / `rdc_waivers` | `sanity.<role>_waivers` | one read-only LLM stage (`Read`, `Grep`, `Glob`; `tb/`, `sva/` denied) decides every violation; the gate is your review | `schemas/<role>_waivers.json`, the table again |
| `cdc_check` / `rdc_check` | `sanity.<role>_check` | code only: the tool again with the waivers sourced; fails unless the open violations are exactly the ones not waived | `sanity/<role>_waive.tcl`, `schemas/<role>_check.json` |

Top module: `options.top` or `schemas/structured_spec.json` `metadata.top_module`; file list `src/rtl/filelist.f`. A missing tool, or a run that wrote
no report, fails the step ("not run"); it never passes. No violation: the review makes no LLM call.

## The shared SDC

The analysis reads `sdc/sdc.tcl`, the same file the FPGA and ASIC flows use (`read_sdc`). The `sdc` step is their step:

- With `sdc/sdc.json` (or none and no `sdc/sdc.tcl`): the FPGA flow's rendering (`sdc_show`, `sdc_set`, `sdc_add`, … work here too).
- With your own `sdc/sdc.tcl` and **no** `sdc/sdc.json`: used as it is; nothing is generated or overwritten.

Resets: `create_reset` for the resets of sdc.json, else the reset inputs of the RTL's top (pyslang), `options.resets` wins. Clocks of an sdc.json without
clock groups become one asynchronous group each; a hand-written SDC is trusted for its own groups. `options.extra_tcl` adds lines after the SDC.
Every `sdc/*.tcl` is an input of the analysis, the review and the check: changing the SDC runs them again.

## The violation table

`sanity/<role>_report.md` (and `.csv`), rewritten by every step of the lane: **No.** (V1…), **Name** (the tool's tag), **Severity**, **Description**,
**Waived** (Yes / No), **Comment**. Right after the analysis nothing is waived and the Comment is empty; after the review the Comment says why a
violation is waived (the rule and the cited evidence), or what happens to it (the RTL fix and whether it was handed off, the spec question, the SDC
request, the question for you and your answer).

## The waiver review

Every violation gets exactly one decision:

| Action | Meaning | Then |
|---|---|---|
| `waive` | safe, under a knowledge rule whose conditions were all verified and cited (`rule`: K3, U1, …) or by a design reason with its evidence | a `waive_cdc` / `waive_rdc` command in `sanity/<role>_waive.tcl`, applied by `<role>_check`. A waiver the report cannot express (no objects named) becomes an `ask`, never an assumption |
| `fix_rtl` | the RTL is unprotected: one module, one structural change | scoped change request `[rtl:<module>] …` for the owner flow's `rtl` step |
| `spec` | the spec owner has to decide | change request for the owner flow's `spec` step |
| `sdc` | a constraint is missing or wrong in the SDC (e.g. a `set_max_delay` on a data path, a clock group, a reset) | **a request to you**: the exact Tcl command and a question; the SDC is yours, the review never edits it |
| `ask` | a condition cannot be verified, no rule covers it | one question for you with 2–4 suggested answers |

`sdc` and `ask` are engine questions (the Questions tab, the picker, `Ops.answer`); open ones hold the gate (`pending_reviews`; `--yes` or auto-approve passes
it and the items stay open). Your answer is saved in `sanity/<role>_answers.json` (by a stable id of the violation: its tag and objects), makes the review
stale, and is given to the LLM as settled. To satisfy an SDC request, change the SDC (`sdc_add what=exception text='max_delay from=A to=B value=15'` when the
SDC is sdc.json's, or edit your file): the analysis runs again on the new SDC.

Changes propagate as updates: a decision stays while the RTL, the SDC and the knowledge it was made on are unchanged; only new violations and the ones
waiting for you go to the LLM again (`options.batch`, default 40 violations per call).

### Knowledge

| File | Holds | Owner |
|---|---|---|
| `q3tui/flows/sanity/cdc_waivers/md/knowledge.md` | built-in CDC rules `K1`…`K5` (valid / data, quasi-static, Gray counter, convergence of error flags, compare-before / after): pattern, conditions, evidence, when to ask | the flow |
| `<project>/sanity/<role>_knowledge.md` | the project's cases `U1`…; they win over the built-in ones (there are no built-in RDC rules) | you |

A rule applies only when **all** its conditions are verified in the RTL, the SDC and the spec and cited. Both files are inputs of the review.

## Hand-off

`sanity_handoff` (`q3tui act sanity_handoff [ids=V1,cdc:V2]`, the assistant, the gate's offer after you approve) sends every `fix_rtl` and `spec` decision
to `options.handoff_flow` (default `vlsit`, per review) as change requests, the way `asic_handoff` does; that flow updates the RTL and this flow's analysis
is stale and runs again. SDC requests and questions are not handed off. The hand-off is recorded in the decision and in the table, not as an edit of the step.

## Tools

Roles `cdc` and `rdc` (`tools.json`; the flow's default is VC Static, `module load synopsys/vc_static`, `/tools/Synopsys/vc_static`), used only when the
project defines no such role. `sanity/<role>_setup.tcl` (after the examples of `doc/vcst/examples/CDC`, `RDC`):

```tcl
cd <project root>
set_app_var enable_cdc true                  ;# enable_rdc for rdc
analyze -format sverilog -vcs "-f src/rtl/filelist.f"
elaborate <top>
read_sdc sdc/sdc.tcl                         ;# the shared SDC
create_reset -sense low i_rst_n              ;# (resets: above)
configure_rdc_corrupt -report_rdc_on_cdc true   ;# rdc only
check_cdc                                    ;# check_rdc
source sanity/cdc_waive.tcl                  ;# only in the check step's run
report_violations -app CDC -verbose -file <work>/cdc_violations.rpt    ;# rdc: report_rdc -list -limit 0 -include_compressed -no_summary -hide_id -file … (compressed items too: the tool folds similar ones)
```

The violations are read from that report (`lib/vcstatic.py`, both report formats), not from the log; infos are counted and dropped. A CDC waiver is the tag plus
the objects (`-from` / `-to`, or the convergence point): VC Static numbers violations per run, so a waiver by `-id` would hit the wrong ones. An RDC waiver is
the tag plus the message. `<role>_check` counts and compares by that same signature: 111 violations with 110 waived end with 1 open, a different count, a
waived violation still reported, an open one that disappeared, or a waiver that cannot be written fails the step (`schemas/<role>_check.json`).
