# The FPGA flow (`fpga`) — PPA estimation on an FPGA part

A built-in folder flow (`q3tui/flows/fpga/`, `pipeline.flow: fpga` or `q3tui --flow fpga`) that takes the RTL the VLSIT flow
produced (`src/rtl/`, read only) and estimates area and timing on an FPGA part. It is for PPA estimation and optimisation,
not for deploying to a board. The vlsit flow is untouched; both can work in one project (their steps have different names).

```
sdc ─► fpga_synth ─► timing_util_report [gate: human] ─► ppa_optimize [gate: human]
```

| Step | Kind | Does | Writes |
|---|---|---|---|
| `sdc` | `fpga.sdc` | `sdc/sdc.json` → the SDC Tcl files (code only) | `sdc/{clocks,clock_groups,exceptions,io_constraints,env,sdc}.tcl` |
| `fpga_synth` | `fpga.fpga_synth` | Tcl script (code) + tool role `fpga_synth` (Vivado: `synth_design -mode out_of_context`) | `fpga/synth.tcl`, `fpga/reports/*.rpt`, `schemas/fpga_synth.json` |
| `timing_util_report` | `fpga.timing_util_report` | reads the reports: LUT / FF / BRAM / DSP, WNS / TNS / Fmax, worst path, unconstrained ports; verdict vs targets | `schemas/ppa_report.json` |
| `ppa_optimize` | `fpga.ppa_optimize` | when a target is missed: one read-only LLM stage proposes fixes (below) | `schemas/ppa_plan.json` |

Top module: `options.top` or `schemas/structured_spec.json` `metadata.top_module`; file list: `src/rtl/filelist.f`
(`options.filelist`). A missing tool fails the step ("not run"), never passes it.

## SDC (`sdc/`)

`sdc/sdc.json` is the source of truth; the Tcl files are rendered from it (a changed file is backed up first):

| File | Holds |
|---|---|
| `clocks.tcl` | `create_clock` (name, port, period, duty), `set_clock_uncertainty` |
| `clock_groups.tcl` | `set_clock_groups` (asynchronous / exclusive); a comment for a single clock |
| `io_constraints.tcl` | `set_input_delay` / `set_output_delay` (max = share of the period, min 0) on every port of the top module except clocks, resets and `io.exclude` |
| `exceptions.tcl` | `set_false_path` from every asynchronous reset, plus false / multicycle / min / max delay exceptions |
| `env.tcl` | `set_driving_cell`, `set_load`, `set_max_fanout`, `set_max_transition` (ASIC tools; the FPGA tools skip it) |
| `sdc.tcl` | sources the above in order (`env.tcl` only where its commands exist), so Design Compiler can `source` the same constraints |

First run: clocks = input ports named like a clock, resets = reset-named inputs, the period from the spec ("target frequency
100 MHz"; `options.period_ns` overrides; 10 ns if the spec says nothing), I/O delay 30 % of the period. After that `sdc.json` is yours.

Editing goes through the flow's actions (`flows/fpga/addon.py`; docs/spec/flows.md "Add-ons"): `sdc_show`, `sdc_replace key text`, `sdc_set key value`
(`clock.i_clk.freq_mhz`, `clock.i_clk.period_ns`, `io.input_pct`, `io.output_pct`, `resets`, `env.load_pf`, …), `sdc_add what text`
(a clock, an exception, a clock group) and `sdc_remove key`. They are assistant tools, `q3tui act sdc_set key=… value=…` and the TUI's
`/act …`; the TUI's **SDC** view on the `sdc` step calls the same ones. It has three columns: the sections in the order of the files,
the items of the highlighted section, and the detail — that section's generated Tcl file. Tab moves between the columns; the bars between them are the app's splitters (drag to resize, double-click to reset,
remembered per project in `.q3tui/tui/layout.json`), like the columns of the other step views (docs/spec/tui.md "Resizing").

| Section (`file`) | Items |
|---|---|
| Clocks (`clocks.tcl`) | one per clock: port, MHz, period, uncertainty, duty |
| Clock groups (`clock_groups.tcl`) | kind and the groups |
| I/O (`io_constraints.tcl`) | input / output delay (% of the period), reference clock, ports left out |
| Exceptions (`exceptions.tcl`) | the asynchronous resets (false paths), then each exception |
| Environment (`env.tcl`) | driving cell, load, max fanout / transition |

Three keys: `e` (or Enter) opens a dialog with all the fields of the highlighted item (`ctrl+s` applies, `Esc` cancels), `a` opens the same
dialog empty to add a clock, clock group or exception, `d` removes the highlighted one. An empty optional field clears it.
The Tcl files are regenerated at once and the sdc and synthesis steps go stale; `r`
re-synthesizes.

## Tools and numbers

`fpga_synth` has a Vivado default (`flows/fpga/tools.json`, module `xilinx/vivado`) used by this flow only when no
`tools.json` / `tools.roles` of the project defines the role. `options.fpga` of the step: `part` (default `xc7k325tffg900-2`), `implement`
(adds opt / place / route), `synth_args`, `defines`, `generics`, `extra_tcl`. Out of context means the block alone, no pins or
wrapper; numbers after synthesis are estimates. Setup slack is judged at every stage, hold slack only after implementation
(before placement the tool has no routing delay: the report shows it but does not judge it).

`timing_util_report` options: `targets: {max_util_pct: {lut: 70, ff: 70}, min_slack_ns: 0}`.

## PPA optimization

Deterministic first: the report says whether a target is missed (setup slack, hold after implementation, a utilization budget);
met → the plan says so and no LLM runs (`options.when: always` asks for improvements anyway; `options.goal` says what to improve).
Otherwise one read-only stage (`Read`, `Grep`, `Glob`; the testbench and SVA are denied) gets the report numbers as ground truth, the
module list, and what earlier rounds tried, and returns a `PpaPlan`: an analysis and up to five fixes, each with a target (`rtl`: one
module, `spec`: the target cannot be met by RTL changes), the change, its evidence in the report, the expected effect and the risk.
Fixes naming an unknown module are dropped. It writes no RTL.

**Hand-off** (`ppa_handoff`, the Fixes tab's `h` / `H`, `q3tui act ppa_handoff [ids=F1,F2]`, the assistant): each fix goes to the flow that
owns the RTL and the spec (`options.handoff_flow`, default `vlsit`) as a change request of its step: `[rtl:<module>] PPA (<part>): …` for
`rtl` (the scoped syntax: only that module is regenerated), a plain request for `spec`. That flow's own rules apply when you run it
(`q3tui --flow vlsit run`: the RTL update, then the checks of its later steps). The new RTL makes this flow's synthesis, report and
`ppa_optimize` stale; they run again, the plan keeps what was tried, and after `options.max_iterations` (default 3) rounds with the
target still missed the step fails and says so — raise the limit, or change the clock target / the spec.

## Not built yet

Quartus and Yosys role presets for `fpga_synth`.

## Where the code is

All of it under `q3tui/flows/fpga/`: `FLOW.md`, one folder per step (`step.py`, `md/skill.md`), `lib/` (SDC model and renderer, Vivado script and
report parsers, step base), `addon.py` (actions), `panels.py` (TUI), `tools.json` (default role). Core has only the generic add-on hooks and the
`vivado` log parser.
