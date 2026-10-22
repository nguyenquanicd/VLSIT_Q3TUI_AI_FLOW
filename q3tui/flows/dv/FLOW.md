---
name: dv
title: DV — spec → requirements → config → testbench (plain SystemVerilog or UVM with an existing VIP)
version: 1
session: fresh
tools:
- sim
- run
sees:
  tb:
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
- tb
---
Design verification on its own: the testbench is written from the spec, the requirements and the configuration alone, never from the RTL (the RTL is a black box DUT). `tb` has two backends (`pipeline.step_options.tb.backend`): `plain_sv` (default: a test plan of test cases, each with the REQs it checks, and a plain SystemVerilog testbench) and `uvm_vip` (a UVM testbench from the approved spec and an existing VIP, with the released RTL integrated as the DUT in a separate phase, compiled and smoke-tested with VCS: docs/spec/dv-flow.md). spec, parse and config are the spec flow's own steps. Needs the RTL from the `rtl` flow in `src/rtl/` for the `uvm_vip` integration phase.
