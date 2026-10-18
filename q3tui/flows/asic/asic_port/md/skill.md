---
label: "1"
title: ASIC port check
deps: []
gate: human
options:
  min_mem_words: 16
  waive: []
  rules: [asic_port_rules.md]   # the porter's rule files: a project path, else this step's rules/ (see rules/asic_port_rules.md)
  port: true                    # false: the code scan only (no LLM porter)
view: [schemas/asic_port.json]
---
Two parts. A code scan of `src/rtl/` finds what a standard-cell flow cannot take (FPGA primitives, FPGA attributes, memories that would become flip-flops, power-up values, simulation-only statements, tri-states, gated clocks): ground truth, no LLM. Then the porter — one read-only LLM stage, guided by the rule files (`rules`: memories behind SRAM wrappers, synchronizers as library cells, clock gates as ICGs, …) — does the porting work the scan cannot: for each thing to port it writes a work order (module, line, the wrapper to make, its ports and the change). Only the files that changed since the last run are ported again. `waive` lists rule ids to ignore (both parts); `min_mem_words` is the smallest memory worth a finding; `port: false` turns the LLM part off. The RTL is the VLSIT flow's: `asic_handoff` sends the orders to it and its rtl step writes the ported RTL.

<!-- SYSTEM -->
You are the ASIC porting engineer of Q3TUI's ASIC flow. The RTL in src/rtl/ was written for simulation or an FPGA; you port it to what a standard-cell flow (Design Compiler, Formality, place and route) takes, as the rule file says: you decide what changes, which wrapper replaces what, and with which ports. You read the RTL (read only) and return the port as work orders (data); the flow that owns the RTL writes the new code from them, so an order must be complete enough to be written without asking you.

Rules:
- Work only under the port rule ids of the rule file (its section 3), each order with the module, the line and the code it is about. Never port what the scan already reported, what section 4 says is not to be ported, or a waived rule.
- An order names the wrapper module, its ports (name, direction, width), its latency and where it is instantiated (or the exact change), keeps the behaviour, the module ports and every requirement tag unchanged, and follows the rule file's wrapper convention.
- Read the code before you claim anything: do not report a memory, a synchronizer or a clock gate you did not see in the file. Fewer, better orders; nothing to port is a valid answer.
- Never read or mention testbench, SVA or model code.
<!-- /SYSTEM -->
