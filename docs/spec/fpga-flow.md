# The FPGA flow (`fpga`) — PPA estimation on an FPGA part

A built-in folder flow (`q3tui/flows/fpga/`, `pipeline.flow: fpga` or `q3tui --flow fpga`) that takes the RTL the rtl flow
produced (`src/rtl/`, read only) and estimates area and timing on an FPGA part (the **part** decides the family and the tool, below). It is for PPA estimation and optimisation,
not for deploying to a board. The spec and rtl flows are untouched; all can work in one project (their steps have different names).

```
fpga_port ─► sdc ─► fpga_synth ─► timing_util_report [gate: human] ─► ppa_optimize [gate: human]
    ▲                   │                   │
    └── every problem ──┴───────────────────┘   (change requests; the fix loop runs the port, then the synthesis again)
```

| Step | Kind | Does | Writes |
|---|---|---|---|
| `fpga_port` | `fpga.fpga_port` | the RTL made ready for the part: a code scan (memory initialization files the part's tool cannot read, another vendor's primitives), then the porter (an LLM with tools, playbook `fpga-port`) when there is something to do — it writes the memory images, changes the synthesis setup, hands RTL changes to the flow that owns the RTL and asks what is yours to decide; every problem of the synthesis comes back here (below) | `src/fpga/` (memory initialization files), `schemas/fpga_port.json`, `schemas/questions/fpga_port.*` |
| `sdc` | `fpga.sdc` | `constraints/sdc/sdc.json` → the SDC Tcl files (code only) | `constraints/sdc/{clocks,clock_groups,exceptions,io_constraints,env,extra,sdc}.tcl` |
| `fpga_synth` | `fpga.fpga_synth` | the family's script (code) + its tool roles: Vivado `synth_design -mode out_of_context`, Yosys + nextpnr, or Quartus Prime (synthesis, the fit with `implement`, the Timing Analyzer) | `build/syn/fpga/synth.tcl` (or `synth.ys`), `build/syn/fpga/reports/*.rpt` + `synth.log`, `schemas/fpga_synth.json` |
| `timing_util_report` | `fpga.timing_util_report` | reads the reports: LUT / FF / BRAM / DSP, WNS / Fmax, worst path, unconstrained ports; verdict vs targets; is the result the design (an LLM judge when code finds removed logic; an implausible result goes to `fpga_port`, and is asked here when the rounds are used up) | `schemas/ppa_report.json`, `schemas/questions/timing_util_report.*` |
| `ppa_optimize` | `fpga.ppa_optimize` | when a target is missed: one read-only LLM stage proposes fixes (below) | `schemas/ppa_plan.json` |

Top module: `options.top` or `schemas/structured_spec.json` `metadata.top_module`; file list: `src/rtl/filelist.f`
(`options.filelist`). A missing tool fails the step ("not run"), never passes it.

## SDC (`constraints/sdc/`)

`constraints/sdc/sdc.json` is the source of truth; the Tcl files are rendered from it (a changed file is backed up first):

| File | Holds |
|---|---|
| `clocks.tcl` | `create_clock` (name, port, period, duty), `set_clock_uncertainty` |
| `clock_groups.tcl` | `set_clock_groups` (asynchronous / exclusive); a comment for a single clock |
| `io_constraints.tcl` | `set_input_delay` / `set_output_delay` (max = share of the period, min 0) on every port of the top module except clocks, resets and `io.exclude`; each reset input also gets a `set_input_delay` (same budget) on the clock whose name ends like the reset (`i_rst_n_pclk` ↔ `i_clk_pclk`), else on the I/O clock — a port with only a false path is "partially constrained" to the CDC tools |
| `exceptions.tcl` | `set_false_path` from every asynchronous reset, plus false / multicycle / min / max delay exceptions |
| `env.tcl` | `set_driving_cell`, `set_load`, `set_max_fanout`, `set_max_transition` (ASIC tools; the FPGA tools skip it) |
| `extra.tcl` | the lines of `sdc.json: extra_tcl`: SDC commands of your own or applied from a review (`sanity_handoff`); only constraint commands (`EXTRA_COMMANDS` in `lib/sdc.py`: `set_input_delay`, `set_output_delay`, `set_false_path`, `set_max_delay`, `set_min_delay`, `set_multicycle_path`, `set_clock_groups`, `set_clock_uncertainty`, …) |
| `sdc.tcl` | sources the above in order (`env.tcl` only where its commands exist, `extra.tcl` last), so Design Compiler can `source` the same constraints |

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

## FPGA port (`fpga_port`)

Like the ASIC flow's `asic_port`, for the chosen part; unlike it, the port also does the porting the FPGA flow owns. **The scan** (code,
`lib/port.py`, ground truth, every run) lists what the part's tool cannot take as the RTL is:

| Rule | Found when |
|---|---|
| `init_missing` | the RTL names a memory initialization file the tool reads — Quartus' `(* ram_init_file = "…" *)` (Quartus only), `$readmemh` / `$readmemb` (every tool) — and it is neither beside the RTL nor in `src/fpga/` (`project.fpga_src_dir`). Quartus Pro stops on it (error 20059); the others synthesize an empty memory, and one nothing writes is removed with what reads it |
| `init_ignored` | `ram_init_file` on a part whose tool ignores it (Vivado, Yosys): the memory starts empty |
| `foreign_primitive` | an instance of another vendor's primitive (`RAMB36E1`, `MMCME2_BASE`, `altsyncram`, `SB_RAM40_4K`, …) |

**The porter** (one LLM stage, fresh; `Read` / `Grep` / `Glob`, `Write` / `Edit` only in `src/fpga/`; the playbook skill `fpga-port`,
yours in `config/skills/fpga-port/` wins) runs when there is something new: a finding of the scan, a change request (the problems handed
back, below), an answer of yours, or `--regenerate`; otherwise its last result stands and nothing is asked. It gets the part and its tool,
the findings, the problems with the tool's own error lines, your decisions, what `src/fpga/` holds, the memory images of the project (the
ones named like the RTL's files first, with their words and width: a dv flow's `build/tb/programs/<program>.imem.hex`, …), the spec and
the RTL to read. Its in-process tools:

| Tool | Does |
|---|---|
| `mem_image` | makes the file the RTL names (`m_imem.mif`) in `src/fpga/` from an image of the project (a `$readmemh` text, a MIF, a binary) with the array's depth × width: a `.mif` target becomes a Quartus MIF (the addresses the image leaves out are 0), others a `$readmemh` text; an image deeper or wider than the memory is refused |
| `synth_setting` | `fpga_synth`'s `top` (a module of the RTL), `defines` (added), `synth_args` (added; Quartus: only `set_global_assignment` / `set_instance_assignment` lines, Vivado / Yosys: only `-flag [value]` — nothing a Tcl interpreter would evaluate), saved to q3tui.yaml. The part is never changed: it is yours |
| `port_scan` | the scan again, to check what it did |

What the RTL has to change for the part becomes a work order (`[rtl:<module>] FPGA port (<family> <part>, <tool>): …`) for the `rtl` step
of the flow that owns the RTL (`options.handoff_flow`, default rtl): handed at once; an unattended run (`--yes`, auto-approve) runs that flow
in its fix loop, otherwise the step stops and says to run it. What only you decide (which program a memory boots with when the spec does not
say and several fit, another part, a spec decision that does not hold here) becomes a question `FP<n>` with the porter's options; the next
run acts on your answer (the answers file is an input of the step). A problem handed back that the porter can do nothing about fails the
step with its reason. The **Port** tab of the step shows the findings, the problems handed back, what was done and the orders.

**Every problem comes back here.** `fpga_synth` hands a failed synthesis (the tool's error lines, its log in
`build/syn/fpga/reports/synth.log`), a memory initialization file the tool would read and is not there (said before the tool runs), and a part
whose family the port has not ported for (it is scanned again; the porter only for what that finds) to `fpga_port` as a change request;
`timing_util_report` hands a result that is not the design (the judgment: summary, causes, the judge's ways). The step marks its fix loop
(docs/spec/pipeline.md "The fix loop"), the engine runs `fpga_port`, then the synthesis and the report again, at most `max_rounds` times in a
run (`fpga_synth` / `timing_util_report` options, Settings; default `pipeline.max_fix_iterations`). Meanwhile `schemas/fpga_synth.json` says
`failed` (with `handed_to`), and the report and `ppa_optimize` wait. Out of rounds, the synthesis fails with the tool's error, and the report
asks you as before. rv32iv: Quartus Pro stopped on `m_imem.mif` / `m_dmem.mif`; the porter made both from the MNIST firmware the spec says
the board runs (`build/tb/programs/mnist.imem.hex` / `.dmem.hex`, 8192 × 32 and 16384 × 128), and the synthesis kept all 2,359,296 bits.

## Families: the part decides (`fpga_synth`)

You pick a **part** (`options.part`: Settings → `fpga_synth` → *Device*, or the project flow's **FPGA** view, which lists the parts and your
spec's); its family, and so the tool, follows from its name (`lib/templates.py` `resolve`). The reports and `ppa_report.json` have the same
names whatever the family, so `timing_util_report`, `ppa_optimize` and the hand-off do not change:

| Family (id) | Parts (by name) | Tool (roles) | Default part | Notes |
|---|---|---|---|---|
| Xilinx 7-series (`xilinx7`) | `xc7…`, `xa7…`, `xq7…` | Vivado batch (`fpga_synth`) | `xc7k325tffg900-2` | out of context; `implement` adds place and route; hold judged then |
| Lattice ECP5 (`ecp5`) | `LFE5U…`, `25k` / `45k` / `85k`, `um-…`, `um5g-…` | Yosys `synth_ecp5` (`fpga_yosys`) + `nextpnr-ecp5` (`fpga_pnr_ecp5`) | `85k`, package `CABGA381`, speed `6` | open source, no licence; out of context; `LFE5UM5G-85F` is nextpnr's `um5g-85k` |
| Lattice iCE40 (`ice40`) | `iCE40…`, `hx…k`, `lp…k`, `up…k` | Yosys `synth_ice40` + `nextpnr-ice40` (`fpga_pnr_ice40`) | `hx8k`, package `ct256` | no out-of-context mode: nextpnr places the IOs; a design that does not fit stops the step |
| Intel Arria 10, Stratix 10, Agilex 7, Cyclone 10 GX | `10AX…` / `10AS…`, `1S…`, `AGF…` / `AGI…`, `10CX…` | Quartus Prime Pro (`fpga_quartus_pro`) | `10AX115N2F40E2LG`, … | below |
| Intel Cyclone V, MAX 10, Cyclone 10 LP / IV | `5CE…` / `5CG…` / `5CS…`, `10M…`, `10CL…` / `EP4CE…` | Quartus Prime Standard (`fpga_quartus_std`) | `5CEBA4F23C7`, … | below |

No part chosen: the default part, with a warning (an older project's `template` setting is still honoured then, and for its own devices).
A part no family here synthesizes (UltraScale, Versal, Gowin, …) fails the step and says why: the flow never silently swaps the part.
Options of the step besides `part`: `package` and `speed` (the open-source families), `implement` (Vivado / Quartus: place and route),
`fpga: {…}` (the Vivado extras below), `frontend: slang` (Yosys), `defines`, `synth_args`. A new family is an entry of `lib/templates.py`
(the parts it owns by name, a tool role, how its numbers are read).

**Quartus** (`lib/quartus.py`). `synth.tcl` (run with `quartus_sh -t`) makes a project in the step's work folder — the RTL, the SDC files, the
RTL folders on the search path, the files the RTL initializes memories from (`ram_init_file`, `$readmemh`; Quartus Pro reads them in its
project folder, so they are copied there: a missing one is a warning before the run, and Pro then stops with error 20059) — and runs the tools
themselves (not the `flow` package): `quartus_syn` (Pro) / `quartus_map` (Standard), `quartus_fit` with `implement`, then `quartus_sta` on
the result (`sta.tcl`: Pro's synthesized snapshot, Standard's post-map netlist, after a fit the final one; it reads the SDC files itself and
writes `Q3TUI WNS / WHS / FROM / TO / LEVELS / CLOCK …` lines, so nothing depends on Quartus' panels). Reports: `utilization.rpt` (the
summary: ALMs, registers, DSP / RAM blocks), `utilization_hier.rpt` (the per-entity table: block memory bits, pins, per instance),
`timing.rpt` (those lines), `clocks.rpt` (the Fmax summary), `paths.rpt`, `synth.log`. The PPA table shows Quartus' own units (ALMs,
registers, block memory bits); hold is judged only after a fit. Checked end to end with Quartus Prime Pro 25.1 (Arria 10, 44 s for a small
design); the Standard edition's script is written the same way but was not run here (the 22.1 Standard install here lacks `libpython3.7m`).
The roles' modules in the preset (`quartus/pro`, `quartus/std`) are placeholders: set yours in the Tools step (here `quartus/25.1pro`).

Yosys + nextpnr: Yosys maps the RTL to the family's cells and writes a JSON netlist; nextpnr places and routes it against the clock
frequencies of the SDC (`--freq` is the fastest clock, ECP5 gets one `FREQUENCY` line per clock when there are several) and reports Fmax per
clock. The reports of `build/syn/fpga/reports/`: `utilization.rpt` (the device utilisation + Yosys `stat`), `timing.rpt` (the last Fmax line of each
clock), `paths.rpt` (the critical path), `pnr.rpt` (nextpnr's whole log), `yosys_stat.rpt`. WNS is the target period minus 1 / Fmax; nextpnr gives no
total negative slack and **no hold analysis** (the report says "n/a"). LUT / FF are `TRELLIS_COMB` / `TRELLIS_FF` (ECP5) and the logic cells
(iCE40; FF from Yosys' `stat`). Numbers across families are comparisons, not predictions for another fabric.

## Tools and numbers

The step `fpga_synth` runs whatever tool the part's family names, one tool role per tool (`Template.roles`): `fpga_synth` (Vivado, Xilinx
7-series), `fpga_yosys` + `fpga_pnr_ecp5` / `fpga_pnr_ice40` (Yosys and nextpnr, the open-source Lattice families), `fpga_quartus_pro` /
`fpga_quartus_std` (Quartus Prime Pro / Standard, Intel). Each role is one command and one log reader, which is why a tool is a role and the
family only picks them. The roles' starting points are in `q3tui/eda/presets/flows/fpga.json` (Vivado: module `xilinx/vivado`; open source:
`oss-cad-suite`; Quartus: placeholders to set), which `q3tui tools init` copies into the project's `config/tools.json`; the flow reads only that file (the project flow's Tools
step adds or edits a role). `options.fpga` of the step: `part` (default `xc7k325tffg900-2`), `implement`
(adds opt / place / route), `synth_args`, `defines`, `generics`, `extra_tcl`. `implement`, `synth_args` (and, for the open-source families, `frontend`) are also flat options of the step, editable in Settings → fpga_synth next to part / package / speed; the flat value wins. The `fpga_synth` step view has an **FPGA** tab with the same settings: Enter on a row changes the part — picked from the device list (the project flow's FPGA view's: the one in use, your spec's, yours, the built-in ones, each with its family's tool; Other… types one) — the family and tool row follows it — or an option; saved to `q3tui.yaml`, then run (r). Out of context means the block alone, no pins or
wrapper; numbers after synthesis are estimates. Setup slack is judged at every stage, hold slack only after implementation
(before placement the tool has no routing delay: the report shows it but does not judge it).

`timing_util_report` options: `targets: {max_util_pct: {lut: 70, ff: 70}, min_slack_ns: 0}`, `judge: auto | always | off`.

**Is the result the design?** (`lib/judge.py`.) Targets alone pass a design the tool trimmed away: rv32iv (an RV32I core, a vector unit, 32 KB
of IMEM / DMEM) synthesized to 48 LUTs, 41 FFs and no block RAM and met every target — its IMEM was initialized only by Quartus' `ram_init_file`,
which Vivado ignores, and its write port was tied off, so Vivado removed it (Synth 8-6014) and folded everything that read it. Code gathers the
evidence from the synthesis log (storage removed, sequential elements removed per instance, outputs driven by a constant, ports nothing reads; Yosys:
unused cells), the memories the RTL declares, the utilization per instance (`report_utilization -hierarchical` → `utilization_hier.rpt`) and the
spec's lines about the target (device, tool, clock); when that points at removed logic or a tiny result (`judge: auto`; `always` judges every
result, `off` none), one read-only LLM stage (`ppa_judge`, the `JUDGE` section of `timing_util_report/md/skill.md`) judges it against the design and
compares the run's part, tool and SDC clocks with the spec's. An implausible result fails the report (`verdict.plausibility`, the **Is it the
design?** row, its causes with whose fix each is: rtl / spec / constraints / synthesis_setup) and **asks you**: a blocking question (`PPA<n>`) whose
pick list is the judge's ways to a real result, its recommendation first (the default for auto-answer). `ppa_optimize` takes the implausible
result as a missed target and your answer as a settled decision. Without credentials the result is "not judged", with a warning. The synthesis
log is copied beside the reports (`build/syn/fpga/reports/synth.log`: `.q3tui/` is hidden from you and the assistant).

**The device.** The project flow's **FPGA** step (docs/spec/project-flow.md) lists the devices the families synthesize, yours
(`config/fpgas.json`) and the parts your spec names, and sets the one in use (`fpga_synth`'s `part`); it warns when the spec names
another part, or one no family here synthesizes.

## PPA optimization

Deterministic first: the report says whether a target is missed (setup slack, hold after implementation, a utilization budget);
met → the plan says so and no LLM runs (`options.when: always` asks for improvements anyway; `options.goal` says what to improve).
Otherwise one read-only stage (`Read`, `Grep`, `Glob`; the testbench and SVA are denied) gets the report numbers as ground truth, the
module list, and what earlier rounds tried, and returns a `PpaPlan`: an analysis and up to five fixes, each with a target (`rtl`: one
module, `spec`: the target cannot be met by RTL changes), the change, its evidence in the report, the expected effect and the risk.
Fixes naming an unknown module are dropped. It writes no RTL.

**Hand-off** (`ppa_handoff`, the Fixes tab's `h` / `H` (they also move the TUI to that flow), `q3tui act ppa_handoff [ids=F1,F2]`, the assistant): each fix goes to the flow that
owns the RTL and the spec (`options.handoff_flow`, default `rtl`) as a change request of its step: `[rtl:<module>] PPA (<part>): …` for
`rtl` (the scoped syntax: only that module is regenerated), a plain request for `spec`. That flow's own rules apply when you run it
(`q3tui --flow rtl run`: the RTL update, then the checks of its later steps). The new RTL makes this flow's synthesis, report and
`ppa_optimize` stale; they run again, the plan keeps what was tried, and after `options.max_iterations` (default 3) rounds with the
target still missed the step fails and says so — raise the limit, or change the clock target / the spec. The count is per SDC: change
the clock (or any constraint) and the rounds start afresh; the earlier ones stay in the plan and in the prompt as "already tried (at 250 MHz)".

**At the gate.** The hand-off is offered where you decide, by the step's `follow_up` (a generic `StepDef` hook: what to offer right after
a gate was approved). Human gate: approving with fixes pending asks "Hand N PPA fix(es) to 'rtl' and switch to it?"; yes hands them off
and moves the TUI to that flow (F does the same later), no keeps them proposed. A gate that approves itself cannot ask: in mode `auto` `on_approve` hands the *RTL* fixes off at once (the other flow is yours to run).
In mode `auto_answer` (or `pipeline.auto_answer`) the run goes on by itself, through the generic `StepDef.unattended` hook: hand the RTL
fixes off, run the owner flow's `rtl` step (`Engine(flow=owner).run(only="rtl", yes=True)`, its gates approved as the unattended run
says), then run this flow again — the new RTL makes `sdc`, synthesis, the report and `ppa_optimize` stale — until the targets are met or
`max_iterations` rounds are used (the step then fails and says so). A spec fix is never handed off unattended (it is your document):
it stays proposed with a warning. The owner flow's later steps (format, style_lint, smoke) are left stale for you to run.

**Function after PPA.** The fixes change the RTL, and nothing in this flow checks that it still works. When the targets are met on RTL that
handed-off fixes changed (`options.verify_after`, default true; once per RTL), the owner flow's functional check runs on it: unattended
(`auto_answer`) the step runs `options.verify_from` → `verify_to` (default `format` → `smoke`) there itself; at a human gate it offers to
switch to that flow (run it there with `r`). If function broke, that flow's own fix loop repairs the RTL — the PPA flow is not told and does
not run again on the repaired RTL (the two are unrelated); the next run of this flow re-synthesizes it. A run that ends anything but complete
(for example the owner's upstream steps are not done) leaves a warning that says so.

**Settings.** The `ppa_optimize` options are in Settings (`,`, opens on the step's tab): Max PPA rounds, Optimize when, Goal, Hand fixes to flow,
Verify after PPA — also `q3tui` assistant `set_settings`, or `pipeline.step_options.ppa_optimize` in q3tui.yaml.

## Not built yet

Gowin, UltraScale+ and Versal families (the part name is recognized and refused with that reason); per-clock frequencies for iCE40 (one
`--freq`); a run of the Quartus Standard script on a working Standard install.

## Where the code is

All of it under `q3tui/flows/fpga/`: `FLOW.md`, one folder per step (`step.py`, `md/skill.md`), `lib/` (SDC model and renderer, Vivado script and
report parsers, the port's scan / memory images / tools in `port.py`, step base), `skills/fpga-port/` (the porter's playbook), `addon.py` (actions), `panels.py` (TUI). Core has only the generic add-on hooks and the
`vivado` log parser.
