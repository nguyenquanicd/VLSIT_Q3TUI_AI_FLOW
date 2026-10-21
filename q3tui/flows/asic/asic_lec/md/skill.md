---
label: "4"
title: Logic equivalence check (Formality)
deps: [asic_synth]
gate: human
options:
  message_filter: []
  extra_tcl: []
view: [build/syn/asic/reports/lec.rpt]
---
Formality: the RTL (reference) against the netlist of `asic_synth` (implementation, with the library and the SVF Design Compiler wrote). Equivalent → pass. Not equivalent or inconclusive is a failed step with the failing points in `build/syn/asic/reports/lec.rpt`; a netlist that differs from the RTL is the synthesis' (or the SDC's) to fix, never waived here. `message_filter` lists RTL interpretation messages (FMR_ELAB-…) you accept as warnings: Formality refuses to link on them otherwise, and the step says which.
