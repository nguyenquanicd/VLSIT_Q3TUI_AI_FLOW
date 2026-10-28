---
name: dv
title: DV — spec → requirements → config → test plan → testbench (plain SystemVerilog, or UVM with a VIP) → test programs → simulation
version: 1
session: fresh
tools:
- sim
- run
- sim_uvm
- run_uvm
- sw_build
- riscv_gcc
- iss
sees:
  testplan:
  - spec
  - parse
  - config
  tb:
  - testplan
  prepare:
  - testplan
  - tb
  simulate:
  - tb
  - prepare
gap_targets:
  spec_gap: spec
  req_gap: parse
  arch_gap: config
steps:
- spec
- parse
- config
- testplan
- tb
- prepare
- simulate
---
Design verification on its own. The testbench is built from the spec, the requirements and the configuration alone, never from the RTL (the RTL is a black box DUT until the testbench is compiled with it). Its kind is the `testbench` option of the `tb` step (Settings → tb): **plain** SystemVerilog (any IP, no VIP) or **UVM** from an existing reusable protocol VIP (AXI-style memory-mapped IPs only so far); `auto` (the default) takes UVM when a VIP location is set and plain otherwise. `testplan` makes what you review before anything is built (a human gate): the test plan (each test case with the requirements it checks, plain) or the verification contract with its open issues (UVM). `tb` builds the testbench and compiles it with the released RTL (plain: tb_top and one file per test case, tool role `sim`; UVM: architecture, VIP binding, scaffold, the agent's regions, then the RTL as the DUT, tool role `sim_uvm`). `prepare` writes and builds the programs and data the plain testbench's tests load (`src/sw/`, tool role `sw_build`: RISC-V GCC + Spike; never the RTL). `simulate` runs the tests and reads every status from the simulator log (plain: `run`, +TC=<id>; UVM: `run_uvm`). spec, parse and config are the spec flow's own steps (docs/spec/dv-flow.md, docs/spec/dut-dv.md). Needs the RTL from the `rtl` flow in `src/rtl/`.
