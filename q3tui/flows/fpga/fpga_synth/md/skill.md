---
label: "2"
title: FPGA synthesis
deps: [sdc]
gate: none
options:
  template: xilinx7   # the FPGA template: xilinx7 (Vivado), ecp5 / ice40 (Yosys + nextpnr) — Settings → fpga_synth → FPGA template
  part: null          # the device; empty: the template's default (xilinx7: xc7k325tffg900-2, ecp5: 85k, ice40: hx8k)
view: [fpga/reports/*.rpt]
---
Out-of-context synthesis of `src/rtl/` with the SDC for the chosen template. Vivado (Xilinx 7-series, 28 nm) writes the utilization / timing / check_timing / paths reports; Yosys + nextpnr (Lattice ECP5 / iCE40, 40 nm, open source) write the same files from their own logs (`pnr.rpt` is nextpnr's whole log). nextpnr has no hold analysis.
