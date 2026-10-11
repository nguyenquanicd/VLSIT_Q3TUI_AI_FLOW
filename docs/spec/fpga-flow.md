# The FPGA flow (`fpga`) — PPA estimation on an FPGA part

A built-in folder flow (`q3tui/flows/fpga/`, `pipeline.flow: fpga` or `q3tui --flow fpga`) that takes the RTL the VLSIT flow
produced (`src/rtl/`, read only) and estimates area and timing on an FPGA part (the **template** picks the family and the tool, below). It is for PPA estimation and optimisation,
not for deploying to a board. The vlsit flow is untouched; both can work in one project (their steps have different names).

```
sdc ─► fpga_synth ─► timing_util_report [gate: human] ─► ppa_optimize [gate: human]
```

| Step | Kind | Does | Writes |
|---|---|---|---|
| `sdc` | `fpga.sdc` | `sdc/sdc.json` → the SDC Tcl files (code only) | `sdc/{clocks,clock_groups,exceptions,io_constraints,env,sdc}.tcl` |
| `fpga_synth` | `fpga.fpga_synth` | the template's script (code) + its tool roles: Vivado `synth_design -mode out_of_context`, or Yosys + nextpnr | `fpga/synth.tcl` (or `synth.ys`), `fpga/reports/*.rpt`, `schemas/fpga_synth.json` |
| `timing_util_report` | `fpga.timing_util_report` | reads the reports: LUT / FF / BRAM / DSP, WNS / Fmax, worst path, unconstrained ports; verdict vs targets | `schemas/ppa_report.json` |
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

The clocks table has two ASIC columns: *ASIC MHz* (the ASIC flow's own frequency for the clock, `e` to set it; `same` = this flow's) and *ASIC Fmax MHz*: the clock the ASIC flow's last synthesis says its library can reach (docs/spec/asic-flow.md; `*` = the RTL changed since, `–` = it has not run), so a frequency you type comes with what an ASIC could do (`E` runs a quick estimate, about a minute, if the ASIC flow has not synthesized yet: docs/spec/asic-flow.md); `sdc_set` and the step's log say it too.

Three keys: `e` (or Enter) opens a dialog with all the fields of the highlighted item (`ctrl+s` applies, `Esc` cancels), `a` opens the same
dialog empty to add a clock, clock group or exception, `d` removes the highlighted one. An empty optional field clears it.
The Tcl files are regenerated at once and the sdc and synthesis steps go stale; `r`
re-synthesizes.

## Templates (`fpga_synth`)

`options.template` picks what `fpga_synth` runs (Settings → `fpga_synth` → *FPGA template*; the same reports and the same `ppa_report.json`
whatever the template, so `timing_util_report`, `ppa_optimize` and the hand-off do not change):

| Template | Family | Tool (roles) | Default device | Notes |
|---|---|---|---|---|
| `xilinx7` (default) | Xilinx 7-series, 28 nm | Vivado batch (`fpga_synth`) | `xc7k325tffg900-2` | the `part` is a Vivado part name; `implement` adds place and route; hold judged then |
| `ecp5` | Lattice ECP5, 40 nm | Yosys `synth_ecp5` (`fpga_yosys`) + `nextpnr-ecp5` (`fpga_pnr_ecp5`) | `85k`, package `CABGA381`, speed `6` | open source, no licence; out of context |
| `ice40` | Lattice iCE40, 40 nm | Yosys `synth_ice40` + `nextpnr-ice40` (`fpga_pnr_ice40`) | `hx8k`, package `ct256` | no out-of-context mode: nextpnr places the IOs; a design that does not fit (LCs, or more IOs than the package) stops the step |

Options of the step: `template`, `part` (empty: the template's default; a part of another family left in the settings is ignored with a
warning), `package` and `speed` (the open-source templates), `fpga: {…}` for the Vivado extras below. For the open-source templates also
`frontend: slang` (the yosys-slang plugin instead of `read_verilog -sv`), `defines`, `synth_args`. New templates are entries of
`lib/templates.py` (a family, a tool role, how its numbers are read).

Yosys + nextpnr: Yosys maps the RTL to the family's cells and writes a JSON netlist; nextpnr places and routes it against the clock
frequencies of the SDC (`--freq` is the fastest clock, ECP5 gets one `FREQUENCY` line per clock when there are several) and reports Fmax per
clock. The reports of `fpga/reports/`: `utilization.rpt` (the device utilisation + Yosys `stat`), `timing.rpt` (the last Fmax line of each
clock), `paths.rpt` (the critical path), `pnr.rpt` (nextpnr's whole log), `yosys_stat.rpt`. WNS is the target period minus 1 / Fmax; nextpnr gives no
total negative slack and **no hold analysis** (the report says "n/a"). LUT / FF are `TRELLIS_COMB` / `TRELLIS_FF` (ECP5) and the logic cells
(iCE40; FF from Yosys' `stat`). Numbers across templates are comparisons, not predictions for another fabric.

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

**Hand-off** (`ppa_handoff`, the Fixes tab's `h` / `H` (they also move the TUI to that flow), `q3tui act ppa_handoff [ids=F1,F2]`, the assistant): each fix goes to the flow that
owns the RTL and the spec (`options.handoff_flow`, default `vlsit`) as a change request of its step: `[rtl:<module>] PPA (<part>): …` for
`rtl` (the scoped syntax: only that module is regenerated), a plain request for `spec`. That flow's own rules apply when you run it
(`q3tui --flow vlsit run`: the RTL update, then the checks of its later steps). The new RTL makes this flow's synthesis, report and
`ppa_optimize` stale; they run again, the plan keeps what was tried, and after `options.max_iterations` (default 3) rounds with the
target still missed the step fails and says so — raise the limit, or change the clock target / the spec. The count is per SDC: change
the clock (or any constraint) and the rounds start afresh; the earlier ones stay in the plan and in the prompt as "already tried (at 250 MHz)".

**At the gate.** The hand-off is offered where you decide, by the step's `follow_up` (a generic `StepDef` hook: what to offer right after
a gate was approved). Human gate: approving with fixes pending asks "Hand N PPA fix(es) to 'vlsit' and switch to it?"; yes hands them off
and moves the TUI to that flow (F does the same later), no keeps them proposed. A gate that approves itself cannot ask: in mode `auto` `on_approve` hands the *RTL* fixes off at once (the other flow is yours to run).
In mode `auto_answer` (or `pipeline.auto_answer`) the run goes on by itself, through the generic `StepDef.unattended` hook: hand the RTL
fixes off, run the owner flow's `rtl` step (`Engine(flow=owner).run(only="rtl", yes=True)`, its gates approved as the unattended run
says), then run this flow again — the new RTL makes `sdc`, synthesis, the report and `ppa_optimize` stale — until the targets are met or
`max_iterations` rounds are used (the step then fails and says so). A spec fix is never handed off unattended (it is your document):
it stays proposed with a warning. The owner flow's later steps (tb, sva, verify, doc) are left stale for you to run.

**Function after PPA.** The fixes change the RTL, and nothing in this flow checks that it still works. When the targets are met on RTL that
handed-off fixes changed (`options.verify_after`, default true; once per RTL), the owner flow's functional check runs on it: unattended
(`auto_answer`) the step runs `options.verify_from` → `verify_to` (default `tb` → `verify`) there itself; at a human gate it offers to
switch to that flow (run it there with `r`). If function broke, that flow's own fix loop repairs the RTL — the PPA flow is not told and does
not run again on the repaired RTL (the two are unrelated); the next run of this flow re-synthesizes it. A run that ends anything but complete
(for example the owner's upstream steps are not done) leaves a warning that says so.

**Settings.** The `ppa_optimize` options are in Settings (`,`, opens on the step's tab): Max PPA rounds, Optimize when, Goal, Hand fixes to flow,
Verify after PPA — also `q3tui` assistant `set_settings`, or `pipeline.step_options.ppa_optimize` in q3tui.yaml.

## Not built yet

Quartus (Intel) and Gowin templates; UltraScale+ and Versal templates (the Vivado template takes any part name, but only 7-series is installed here); per-clock
frequencies for iCE40 (one `--freq`).

## Where the code is

All of it under `q3tui/flows/fpga/`: `FLOW.md`, one folder per step (`step.py`, `md/skill.md`), `lib/` (SDC model and renderer, Vivado script and
report parsers, step base), `addon.py` (actions), `panels.py` (TUI), `tools.json` (default role). Core has only the generic add-on hooks and the
`vivado` log parser.
