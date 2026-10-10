---
name: fpga
title: FPGA PPA estimation and optimization — SDC → synthesis → timing / utilization report → fixes
version: 1
session: fresh
tools:
- fpga_synth
steps:
- sdc
- fpga_synth
- timing_util_report
- ppa_optimize
---
PPA estimation on an FPGA part, not a board deployment. Input: the RTL of the VLSIT flow (`src/rtl/`, read only) and the constraints of `sdc/` (clocks, I/O delays, exceptions: edit `sdc/sdc.json` or use the SDC tab). Synthesis is out of context (the block alone, no pins), so numbers are estimates after synthesis. `ppa_optimize` proposes RTL / spec fixes when a target is missed; `ppa_handoff` sends them to the flow that owns the RTL.
