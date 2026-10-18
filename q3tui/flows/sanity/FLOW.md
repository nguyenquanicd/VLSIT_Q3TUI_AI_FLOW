---
name: sanity
title: Sanity checks — SDC → CDC / RDC analysis → waiver review → check, per lane
version: 1
session: fresh
tools:
- cdc
- rdc
steps:
- sdc
- cdc
- cdc_waivers
- cdc_check
- rdc
- rdc_waivers
- rdc_check
---
Sanity checks on the RTL the VLSIT flow produced (`src/rtl/`, read only) with the SDC the FPGA and ASIC flows share (`sdc/`). Each lane (CDC, RDC) runs the Synopsys VC Static tool (`cdc` / `rdc`), keeps every violation as a table (No., Name, Severity, Description, Waived, Comment), lets a read-only LLM review decide each one (waive with the evidence, fix the RTL, ask the spec, change the SDC — a request to you —, or ask you), and checks the waivers by running the tool again (`cdc_check` / `rdc_check`). `sanity_handoff` sends the RTL / spec fixes to the flow that owns them, as the ASIC flow does. GCA is not in this flow yet (no tool here).
