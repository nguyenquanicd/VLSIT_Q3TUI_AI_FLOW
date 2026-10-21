---
label: "1"
title: SDC constraints
deps: []
gate: none
options: {}
view: [constraints/sdc/sdc.tcl]
---
The SDC the analysis reads, shared with the FPGA and ASIC flows. With `constraints/sdc/sdc.json` it is the FPGA flow's `sdc` step (renders `constraints/sdc/*.tcl`). With your own `constraints/sdc/sdc.tcl` and no `constraints/sdc/sdc.json` it is used as it is. The waiver review never edits it: when a constraint is missing it asks you (a request with the command, `sdc_add` / `sdc_set` when the SDC is `sdc.json`'s).
