---
name: rtl
title: RTL generation — spec, rules → requirements → config → CSR → RTL → format → style lint → smoke test
version: 1
session: fresh
tools:
- lint
- synth
- sim
- run
- verible_lint
- verible_format
- csr_gen
sees:
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
- rules
- parse
- config
- csr
- rtl
- format
- style_lint
- smoke
---
RTL from a spec, ready for the verification flow. spec, parse, config and rtl are the VLSIT flow's own steps (same artifacts, same questions, same update-not-re-run), the RTL goes to `rtl/`. Then `format` (Verible format, whitespace only) and `style_lint` (Verible lint with flags written from the rules, judged by the LLM; fixes go back to `rtl` as scoped change requests, so the chain generate → format → check → fix → format → check runs by itself), and `smoke` (a basic-function testbench written from the spec alone, compiled with the RTL and run). Each flow is independent of the `vlsit` flow: pick one with `pipeline.flow`.

The rules: `rules/rtl_rules.md` of the project (yours: wins) over Q3TUI's default `vlsit_rtl_rule_default.md`. The generator follows them and the style checker turns them into Verible flags.
