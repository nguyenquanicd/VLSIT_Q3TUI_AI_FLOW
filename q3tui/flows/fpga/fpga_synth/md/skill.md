---
label: "3"
title: FPGA synthesis
deps: [sdc, fpga_port]
gate: none
options:
  part: null          # the device: its family decides the tool (Settings → fpga_synth, or the project flow's FPGA view); empty: the default part
  max_rounds: null    # how often a failed synthesis is handed to fpga_port and run again in one run (empty: pipeline.max_fix_iterations)
view: [build/syn/fpga/reports/*.rpt]
---
Synthesis of `src/rtl/` with the SDC for the chosen part (the memory initialization files the RTL names: beside it or in `src/fpga/`). A failed synthesis is handed to `fpga_port` with the tool's error lines, and runs again after it. Vivado (Xilinx 7-series, 28 nm) writes the utilization / timing / check_timing / paths reports; Yosys + nextpnr (Lattice ECP5 / iCE40, 40 nm, open source) write the same files from their own logs (`pnr.rpt` is nextpnr's whole log). nextpnr has no hold analysis.
