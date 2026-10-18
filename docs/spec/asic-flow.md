# The ASIC flow (`asic`) — synthesis to a standard-cell netlist and its equivalence check

A built-in folder flow (`q3tui/flows/asic/`, `pipeline.flow: asic` or `q3tui --flow asic`) that takes the RTL the VLSIT flow produced
(`src/rtl/`, read only), synthesizes it with Design Compiler to a standard-cell library, and proves the netlist equal to the RTL with
Formality. It is the "ASIC synthesis" box of the flow map (ASIC port → ASIC synthesis → LEC). The vlsit flow is untouched; the flows
share `sdc/` (the same files the FPGA flow writes) and hand work to each other through change requests.

```
asic_port [gate: human] ─┐
                         ├─► asic_synth [gate: human] ─► asic_lec [gate: human]
sdc ─────────────────────┘
```

| Step | Kind | Does | Writes |
|---|---|---|---|
| `asic_port` | `asic.asic_port` | code scan of `src/rtl/` for what an ASIC cannot take, then the porter (one read-only LLM stage, `rules/asic_port_rules.md`): the porting work the scan cannot do (SRAM wrappers, synchronizer cells, ICGs, …) as work orders for the RTL | `schemas/asic_port.json` |
| `sdc` | `asic.sdc` | the FPGA flow's SDC step: `sdc/sdc.json` → `sdc/*.tcl` | `sdc/*.tcl` |
| `asic_synth` | `asic.asic_synth` | Tcl script (code) + tool role `asic_synth` (`dc_shell -f`): compile, netlist, reports, numbers against targets | `asic/synth.tcl`, `asic/netlist/<top>.{v,ddc,sdc,svf}`, `asic/reports/*.rpt`, `schemas/asic_synth.json` |
| `asic_lec` | `asic.asic_lec` | Tcl script (code) + tool role `asic_lec` (`fm_shell -f`): RTL vs netlist | `asic/lec.tcl`, `asic/reports/lec.rpt`, `schemas/asic_lec.json` |

Top module: `options.top` or `schemas/structured_spec.json` `metadata.top_module`; file list: `src/rtl/filelist.f`. A missing tool or library
fails the step ("not run"), never passes it.

## ASIC port (`asic_port`)

Two parts. **The scan** is deterministic and ground truth (comments and `// synopsys translate_off` regions skipped); each finding has an id
(`P1`…), the rule, the module, the line, the code, the fix that belongs to it and `source: code`:

| Rule | Severity | Finds |
|---|---|---|
| `vendor_primitive` | error | instantiated FPGA primitives (`xpm_*`, `RAMB*`, `DSP48*`, `BUFG`, `FDRE`, `LUT6`, `altsyncram`, …) |
| `sim_only` | error | `$display`, `$finish`, `$random`, `#delays`, `force` / `release`, `fork`, `wait` |
| `fpga_attribute` | warn | `ram_style`, `use_dsp`, `keep`, `mark_debug`, `async_reg`, `max_fanout`, `syn_*`, … |
| `memory_array` | warn | unpacked arrays of `min_mem_words` or more (default 16) — flip-flops in standard cells; wants an SRAM / register-file macro |
| `mem_init` | warn | `$readmem[hb]` |
| `initial_value` | warn | `initial` blocks and declaration initialisers (no power-up value in an ASIC) |
| `tristate` | warn | `'z` drivers |
| `gated_clock` | warn | a clock assigned from logic |

**The porter** is one read-only LLM stage (`Read`, `Grep`, `Glob`; the testbench, SVA and models denied) that reads the RTL with the rule
files of `options.rules` (default `asic_port_rules.md`, in the step's `rules/`; a project path wins) and does the porting the scan cannot do:
it decides what changes, which wrapper replaces what and with which ports, and writes that as work orders (findings with `source: llm`, a
`wrapper` module and a `fix` the RTL author can write without asking). The rule file has the wrapper convention (a structure a macro or a library cell implements sits behind one small
`m_<function>` module with fixed ports), the scan's rules (not reported again) and the port rules: `mem_sram_wrapper` (memories →
SRAM / register-file / ROM wrappers), `sync_lib_cell` (synchronizers → library cells behind `m_sync_*`), `clock_gate_icg`,
`clock_mux_divider`, `pll_macro_wrapper`, `pad_wrapper`, `latch_inferred`, `async_reset_style`, `fpga_only_branch`. Code checks the answer:
an unknown rule id, an unknown module or a waived rule is dropped (and said in `dropped`), and a line the scan already has keeps the
scan's finding. The porter follows "changes propagate as updates": `asic_port.json` records each file's hash and the rules / options key,
and only the files whose text changed (or all of them, when a rule file or option changed) go back to the LLM; the others keep their findings.
`options.port: false` runs the scan only. The step writes no RTL: the RTL is the VLSIT flow's, so the orders go there (`asic_handoff`) and its rtl step writes the ported code.

The gate is your review. `options.waive: [rule ids]` ignores rules (both parts), `options.min_mem_words` sets the memory threshold. **Hand-off**
(`asic_handoff`, the Findings tab's `h` / `H` — they also move the TUI to that flow, `q3tui act asic_handoff [ids=P1,P2]`, the
assistant): every finding goes to the flow that owns the RTL (`options.handoff_flow`, default `vlsit`) as a scoped change request of its
`rtl` step: `[rtl:<module>] ASIC port (<file>:<line>, <rule title>): <code> — <fix>`. Run that flow to apply them; this flow's steps are then stale and run again.

## Synthesis (`asic_synth`)

`options`: `library` (the `.db` of the standard cells; a list for several; `~` and `$ENV` expand), `compile` (default `compile_ultra`),
`max_paths`, `extra_tcl` (lines after `link`: `set_max_area`, `set_dont_use`, operating conditions, …), `defines`, and the targets
`min_slack_ns` (default 0) and `max_area_um2` (none). The default library is the GF180MCU 7-track 5 V cell library's typical corner
(`/tools/PDK/GF180/…/build/synopsys/gf180mcu_fd_sc_mcu7t5v0__tt_025C_5v00.db`); change it in **Settings** (`,` on the `asic_synth`
step: Library, Compile command, Min setup slack, Max area), through the assistant, or `pipeline.step_options.asic_synth.library` in q3tui.yaml. It is one choice for both flows, made by **technology, then corner** from the catalog `pdks.json` (only technologies whose `.db` files exist are offered). The built-in catalog (`q3tui/flows/asic/pdks.json`) lists **open-source PDKs only**: GF180MCU 7-track 5 V (180 nm) and FreePDK45 OSU gscl45nm (45 nm), at this site's paths. A licensed PDK (TSMC, SAED, a foundry's) goes in **your project's** `<project>/pdks.json` (same format; added to the built-in ones, an entry with the same `id` replaces one, `"only": true` hides the built-in ones) — never in the repo; the sample project has one for the TSMC 65 nm and SAED 14 nm libraries installed here. The clock edit dialog of the SDC tab (either flow) has *ASIC technology* and *ASIC library corner* (the corners follow the technology; the typical corner is picked with it), and the FPGA flow's Settings has the same *ASIC library (.db)* as a path (an `asic_synth` tab) for a library that is not in the catalog (the dialog then shows the path). The choice is saved for both flows, and the **Estimate ASIC Fmax** button uses what the form shows. The Fmax hint names it ("GF180MCU 7-track 5 V · tt_025C_5v00").

The script sets the SVF first (Formality needs the guidance for what `compile` changes), analyzes / elaborates / links the RTL,
runs `extra_tcl`, sources `sdc/sdc.tcl`, compiles, writes the netlist (`change_names -rules verilog`), the `.ddc`, the SDC, and the
reports `area`, `timing` (the worst paths), `qor`, `power`, `constraints` (`-all_violators`), `check_design`, `check_timing`.
**The ASIC's own clock.** The flows share `sdc/sdc.json`, but each clock can have its own ASIC frequency: the *ASIC MHz* column of the SDC tab (`e` on the clock: *ASIC frequency*; empty / `same` = the FPGA's frequency), or `sdc_set key=clock.<name>.asic_freq_mhz value=250` (the assistant, `q3tui act`). It is a constraint, so it lives in `sdc.json` with the others, not in Settings. `asic_synth` renders the shared constraints with those clocks changed into `asic/sdc/` and Design Compiler sources that; `sdc/` (the FPGA's) is not touched. A change makes only the ASIC synthesis stale. `asic_synth.json` records `clock_override`.

**Quick estimate (`asic_estimate`).** The Fmax above needs the full synthesis (minutes). For the number next to the frequency you type, `asic_estimate` (`E` in the SDC tab, the **Estimate ASIC Fmax** button of a clock's edit dialog — the answer shows in the dialog, next to the *ASIC frequency* field you set — `q3tui act asic_estimate`, the assistant; registered in the FPGA flow too) runs Design Compiler at low effort (`compile -map_effort low`, about a minute) with only the clocks constrained at a probe period the library cannot meet — 1 ns, or the `probe_ns` of the technology in `pdks.json` (0.15 ns for SAED 14 nm), because the tool's result at a moderate violation is erratic on a fast library: when the library meets the probe, the next run uses half of the period its worst path needed (up to three runs), and the best run of each clock counts (no I/O delays: they scale with the period): the worst register-to-register path then shows as a violation and the Fmax is `1000 / (probe − slack)`. It uses the library of `asic_synth` and writes `schemas/asic_estimate.json`, no netlist. Low effort maps a bit slower than `compile_ultra`, so it leans pessimistic (sample block, GF180: 240 MHz estimated in 59 s; the full run met 240 and estimated 254 from a missed 300; SAED 14 nm RVT tt: about 4.3 GHz in 85 s). The hint shows a fresh synthesis first, then a fresh estimate (`~` in the column, "quick estimate" in the note), then an older result marked with `*`.

**Expected frequency:** for each clock path group the report gives the Fmax the library allows with the worst setup path as it is: `1000 / (period − slack)` (`timing.fmax_mhz`, `timing.groups`; the QoR tab's *Expected Fmax* row, and the step's log line), so a clock set to 300 MHz in the SDC that the PDK cannot reach says "expected Fmax ≈ 254 MHz". It is an estimate from the current netlist (the I/O delays scale with the period, and a looser clock lets the tool trade area for speed), good for choosing the next clock target; synthesize again at that clock to confirm. The number is also shown where you set the frequency: the FPGA flow's SDC tab has an *ASIC Fmax MHz* column on the clocks (`≥`: the ASIC met the clock it was given and can go faster, `*`: the RTL changed since that run, `–`: the ASIC flow has not run), and the `sdc` step's log and `sdc_set` say "i_clk: ASIC ≈ 254 MHz (tt_025C_5v00)" (`lib/hints.py` reads `schemas/asic_synth.json`; it runs no tool).

`schemas/asic_synth.json` has the numbers and the verdict (`timing`, `area`, `constraints`, `overall`). There is no wire-load model or
placement: these are post-synthesis numbers, and power has no switching activity (the default estimate).

## Equivalence (`asic_lec`)

Formality reads the RTL as the reference and the netlist (with the library) as the implementation, sets the SVF, runs `match` and `verify`.
Equivalent → done. Not equivalent, inconclusive or no verdict is a failed step, never waived (the failing points are in
`asic/reports/lec.rpt`). When Formality refuses to link the RTL on an interpretation message (for example `FMR_ELAB-147`, an index that
may leave an array), the step names the message: fix the RTL, or accept it with `options.message_filter: [FMR_ELAB-147]`.

## Tools

Default roles (`flows/asic/tools.json`, used by this flow only when the project defines none): `asic_synth` = `dc_shell -f {script}` with
module `synopsys/syn/V-2023.12` (log parser `dc`), `asic_lec` = `fm_shell -f {script}` with module `synopsys/formality/V-2023.12`.
Define your own `asic_synth` / `asic_lec` roles in `tools.json` / `tools.roles` for other versions or tools.

## Not built yet

GCA and its waivers (the Sanity lane of the flow map), the convergence gate, handing timing failures back to the spec, other synthesis
tools (Genus, Yosys), multiple corners.

## Where the code is

All of it under `q3tui/flows/asic/`: `FLOW.md`, one folder per step (`step.py`, `md/skill.md`), `lib/` (port rules, Design Compiler script and
report readers, Formality script and reader), `addon.py` (settings, `asic_handoff`, the SDC actions), `panels.py` (TUI), `tools.json`. The SDC
step, its actions and its editor are the FPGA flow's. Core has only the flow-map box.
