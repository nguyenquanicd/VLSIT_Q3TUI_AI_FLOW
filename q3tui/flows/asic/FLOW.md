---
name: asic
title: ASIC synthesis — ASIC port check → SDC → Design Compiler → logic equivalence check
version: 1
session: fresh
tools:
- asic_synth
- asic_lec
steps:
- asic_port
- sdc
- asic_synth
- asic_lec
---
ASIC synthesis of the RTL the VLSIT flow produced (`src/rtl/`, read only) with a standard-cell library: `asic_port` finds what an ASIC cannot take (FPGA primitives and attributes, memories that need macros, simulation-only code), `sdc` renders the constraints, `asic_synth` runs Design Compiler (netlist + area / timing / power), `asic_lec` proves the netlist equals the RTL (Formality). The library is a setting (`asic_synth` tab of Settings, or `options.library`).
