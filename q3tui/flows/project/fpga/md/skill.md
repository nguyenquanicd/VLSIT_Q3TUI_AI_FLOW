---
label: "4"
title: FPGA
deps: []
gate: none
options: {}
view: [config/fpgas.json]
---
The FPGA devices the FPGA flow can synthesize for, and the one it uses: the devices of its families (Xilinx 7-series with Vivado, Lattice ECP5 / iCE40 with Yosys + nextpnr, Intel Arria 10 / Stratix 10 / Agilex / Cyclone 10 GX with Quartus Prime Pro, Cyclone V / MAX 10 / Cyclone 10 LP with Quartus Prime Standard), the parts you add (`config/fpgas.json`) and the parts your spec names (found by code). The family, and so the tool, of a part is worked out from its name. `u` uses the highlighted part (the FPGA flow's part, saved to q3tui.yaml), `a` adds a part, Enter or `e` edits yours, `d` removes it. A part no family here synthesizes (UltraScale, Versal) is listed, not usable.
