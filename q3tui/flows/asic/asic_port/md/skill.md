---
label: "1"
title: ASIC port check
deps: []
gate: human
options:
  min_mem_words: 16
  waive: []
view: [schemas/asic_port.json]
---
Scans `src/rtl/` for what a standard-cell flow cannot take (code, no LLM): FPGA primitives, FPGA attributes, memories that would become flip-flops, power-up values, simulation-only statements, tri-states, gated clocks. `waive` lists rule ids to ignore; `min_mem_words` is the smallest memory worth a finding. `asic_handoff` sends the findings to the flow that owns the RTL.
