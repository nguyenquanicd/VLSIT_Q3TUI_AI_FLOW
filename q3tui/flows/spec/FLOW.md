---
name: spec
title: Spec — intent → spec → requirements → configuration
version: 1
session: fresh
gap_targets:
  spec_gap: spec
  req_gap: parse
  arch_gap: config
steps:
- spec
- parse
- config
---
From an idea (or your own spec) to the requirements (`REQ-NNN`, `schemas/structured_spec.json`) and the locked configuration (`schemas/final_config.json`). Human gate: 1 (parse). Gate 2 (config) signs itself when its checks pass. The RTL, testbench and verification flows start from these artifacts; the `rtl` flow reuses these three steps.
