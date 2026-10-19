---
name: vlsit
title: VLSIT RTL generator — spec → requirements → config → RTL ‖ TB → SVA → verification
  → documents
version: 1
session: flow
tools:
- lint
- synth
- sim
- run
sees:
  tb:
  - spec
  - parse
  - config
  rtl:
  - spec
  - parse
  - config
gap_targets:
  spec_gap: spec
  req_gap: parse
  arch_gap: config
steps:
- spec
- parse
- config
- rtl
- tb
- sva
- verify
- doc
---
Human gates: 1 (parse), 3b (sva), 5 (verify). Gates 2 (config) and 4 (tb) sign themselves when their checks pass. No architecture and no golden model: the checks are SVA, the plain-SystemVerilog testbench and mutation testing.

