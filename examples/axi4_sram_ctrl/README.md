# axi4_sram_ctrl worked example (UVM / VIP testbench backend)

**Status: milestones M1-M9 done (M10 final docs open).** This example grows with the milestones of docs/spec/dut-dv.md. Nothing below is claimed
as working until its row in the status table says `done` with the command that was run.

## What this is

`axi4_sram_ctrl` (top `m_vlsi_axi4_sram`) is an AXI4 slave that reads and writes synchronous SRAM banks. The example runs
Q3TUI's `tb` step with the optional `uvm_vip` backend: the testbench is a UVM environment built from the approved
specification and an existing AXI VIP. The existing plain-SystemVerilog backend is untouched (`backend: plain_sv`, the default).

## Golden specification and RTL independence

- Golden behavior: the specification in `spec/` and the artifacts the flow derived from it (`schemas/structured_spec.json`,
  `schemas/final_config.json`, approved at their gates).
- **The RTL in `rtl/` is the DUT only.** It is never given to the behavioral stages (intent, architecture, scaffold, scoreboard,
  tests, coverage). It is read by code in the separate integration phase, after the verification contract is frozen, for the top
  module, ports, parameters and the file list. A spec/RTL difference is reported as a conformance finding; expected behavior never
  changes to match the RTL (rules DUT-DV-001..006, 026..030 in `q3tui/flows/vlsit/tb/rules/vlsit_dut_dv_rule.md`).

## Layout

| Path | Content | Status |
|---|---|---|
| `q3tui.yaml` | selects `backend: uvm_vip`, VIP and integration inputs | done (M1) |
| `tools.json` | VCS tool roles (`sim_uvm`, `run_uvm`, ...) | done (M7/M8); VCS X-2025.06 |
| `spec/`, `schemas/` | specification (`spec/spec.md`) and the approved `structured_spec.json`, `final_config.json` | done (M2); copied from the Design-flow project `axi4_sram_ctrl`, the `rtl_modules` / `modules` fields they carry are excluded as non-behavioral |
| `rtl/` | the released RTL of the Design flow (copied from its `src/rtl`, DUT only) and its file list. The specification parameters were renamed to the RTL's `PARA_*`, so conformance is clean without a mapping | done (M6) |
| `vip/` | VIP manifest (location, revision); the VIP itself is not copied here. Downloaded for this work: `dv_flow/vip_downloads/axi4_avip_mbits-mirafra` (MIT, commit 86b46171) | M4 |
| `schemas/vip_integration.json` | the probe of axi4_avip @ 86b46171 and its binding to this DUT (portable path `$AXI_VIP_HOME`) | done (M4) |
| `schemas/{golden_manifest,verification_contract,tb_architecture}.json` | the reviewed baseline of milestone M2 (one live `tb_intent` run with claude-sonnet-5-5, then your review) | done |
| `schemas/questions/tb.answers.json`, `schemas/dv_review_decisions.json` | your answer to Q-TB-OI-006 (4 KB crossing test) and your review of OI-001..005 (defaults accepted) | done |
| `expected/dv_review_report.md` | the review report of that baseline | done |
| `expected/` (rest) | known expectations used by the regression | M9 |

## Verification status (release V0.1)

| Item | Status | Note |
|---|---|---|
| TC-001 `dut_smoke_test` | **PASS** | VCS, real RTL and VIP: 8 checks, 0 mismatches, UVM_ERROR 0, UVM_FATAL 0 |
| TC-002 .. TC-011 (directed, negative, random) | **todo** | contract `milestone: later`; regions are `DV_TODO` stubs, not run |
| TC-012 INCR burst crossing 4 KB (approved by you, Q-TB-OI-006) | **todo** | in the contract with its expected behavior; not implemented, not run |
| Scoreboard rules SBR-004..007, SBR-009 (+ `.sram`) | **todo** | not implemented (SBR-001, 002, 003 and 008 are implemented and run in the smoke test; SBR-008 is checked on the SRAM cycles the monitor observes, not on idle cycles) |
| Functional coverage COV-001..009 | **todo** | coverage is off |
| Assertions | **todo** | stub region |

A `todo` item is never reported as PASS: running its test prints `DV_TODO` and ends `INCOMPLETE` (DUT-DV-023/024). The smoke PASS shows the
flow works end to end; it is not a claim that the DUT is verified beyond that test.

## Known limitations

Smoke run with the real RTL: `dut_smoke_test` PASS (8 checks, 0 mismatches, UVM_ERROR 0, UVM_FATAL 0). Not yet: functional coverage, the remaining `DV_TODO` regions, constrained-random and stress tests, a committed copy of the generated testbench (M9).

## Run it

```bash
export AXI_VIP_HOME=<checkout of mbits-mirafra/axi4_avip @ 86b46171>/    # the existing VIP, read-only
module load synopsys/vcs/X-2025.06
cd examples/axi4_sram_ctrl && q3tui run --only tb    # contract -> scaffold -> (agent regions) -> VIP -> RTL -> VCS compile -> smoke run
```

The folder is a finished run: `src/tb/` is the generated testbench (agent regions implemented once by a live agent), `.q3tui/pipeline.json`
records what it was built from (so a re-run changes nothing and calls no LLM), `docs/` has the review, architecture and conformance reports.
`schemas/dv_evidence.json` is not committed (it holds the machine-specific run commands); a run writes it and reads the PASS from the log.
Regression: `Q3TUI_VCS_MODULE=synopsys/vcs/X-2025.06 AXI_VIP_HOME=... uv run pytest -m vcs` (the end-to-end test runs this example on a copy).

| Item | Where |
|---|---|
| spec / approved config | `spec/`, `schemas/structured_spec.json`, `schemas/final_config.json` |
| verification contract, review | `schemas/verification_contract.json`, `schemas/dv_review*.json`, `docs/dv_review_report.md` |
| TB architecture | `schemas/tb_architecture.json`, `docs/tb_architecture.md` |
| generated UVM TB | `src/tb/` |
| VIP probe and binding | `schemas/vip_integration.json` |
| RTL integration and conformance | `schemas/dut_integration.json`, `docs/dut_conformance_report.md`, `src/tb/integration/` |
| evidence | `schemas/dv_evidence.json` (written by a run) |
