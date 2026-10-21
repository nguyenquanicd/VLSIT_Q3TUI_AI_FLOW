---
label: "3"
title: ASIC synthesis (Design Compiler)
deps: [asic_port, sdc]
gate: human
options:
  library: /tools/PDK/GF180/globalfoundries-pdk-libs-gf180mcu_fd_sc_mcu7t5v0/build/synopsys/gf180mcu_fd_sc_mcu7t5v0__tt_025C_5v00.db
  compile: compile_ultra
  min_slack_ns: 0
  max_area_um2: null
view: [build/syn/asic/reports/*.rpt, build/syn/asic/netlist/*.v]
---
Design Compiler on the RTL with `constraints/sdc/sdc.tcl` and the library `library` (a `.db`, or a list of them: Settings → asic_synth, or `options.library`). Writes the netlist (`build/syn/asic/netlist/<top>.v`, `.ddc`, `.sdc`, and the SVF Formality needs) and the area / timing / QoR / power / constraint reports. The clock is the SDC's (the FPGA flow's); a clock with an *ASIC MHz* in the SDC tab gets that frequency here instead (rendered into `build/syn/asic/sdc/`, the FPGA's `constraints/sdc/` is not touched). The gate is your review of the numbers against `min_slack_ns` and `max_area_um2`.
