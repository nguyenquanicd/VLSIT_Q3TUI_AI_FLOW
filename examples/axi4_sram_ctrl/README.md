# axi4_sram_ctrl worked example (UVM / VIP testbench: the `dv` flow)

**Status: milestones M1-M9 done (M10 final docs open).** This example grows with the milestones of docs/spec/dut-dv.md. Nothing below is claimed
as working until its row in the status table says `done` with the command that was run.

## What this is

`axi4_sram_ctrl` (top `m_vlsi_axi4_sram`) is an AXI4 slave that reads and writes synchronous SRAM banks. The example runs
Q3TUI's `dv` flow (its `testplan`, `tb` and `simulate` steps, here without the spec flow's): the testbench is a UVM environment built from the approved
specification and an existing AXI VIP, then the RTL is integrated as the DUT and simulated.

## Golden specification and RTL independence

- Golden behavior: the specification in `spec/` and the artifacts the flow derived from it (`schemas/structured_spec.json`,
  `schemas/final_config.json`, approved at their gates).
- **The RTL in `src/rtl/` is the DUT only.** It is never given to the behavioral stages (intent, architecture, scaffold, scoreboard,
  tests, coverage). It is read by code in the separate integration phase, after the verification contract is frozen, for the top
  module, ports, parameters and the file list. A spec/RTL difference is reported as a conformance finding; expected behavior never
  changes to match the RTL (rules DUT-DV-001..006, 026..030 in `q3tui/flows/dv/tb/rules/vlsit_dut_dv_rule.md`).

## Layout

| Path | Content | Status |
|---|---|---|
| `q3tui.yaml` | the `uvm_vip` options of the `tb` step (read by the three dv steps): VIP and integration inputs | done (M1) |
| `config/tools.json` | VCS tool roles (`sim_uvm`, `run_uvm`, ...) | done (M7/M8); VCS X-2025.06 |
| `spec/`, `schemas/` | specification (`spec/spec.md`) and the approved `structured_spec.json`, `final_config.json` | done (M2); copied from the Design-flow project `axi4_sram_ctrl`, the `rtl_modules` / `modules` fields they carry are excluded as non-behavioral |
| `src/rtl/` | the released RTL of the Design flow (copied from its `src/rtl`, DUT only) and its file list. The specification parameters were renamed to the RTL's `PARA_*`, so conformance is clean without a mapping | done (M6) |
| `vip/` | VIP manifest (location, revision); the VIP itself is not copied here. Downloaded for this work: `dv_flow/vip_downloads/axi4_avip_mbits-mirafra` (MIT, commit 86b46171) | M4 |
| `schemas/vip_integration.json` | the probe of axi4_avip @ 86b46171 and its binding to this DUT (portable path `$AXI_VIP_HOME`) | done (M4) |
| `schemas/{golden_manifest,verification_contract,tb_architecture}.json` | the reviewed baseline of milestone M2 (one live `tb_intent` run with claude-sonnet-5-5, then your review) | done |
| `schemas/questions/tb.answers.json`, `schemas/dv_review_decisions.json` | your answer to Q-TB-OI-006 (4 KB crossing test) and your review of OI-001..005 (defaults accepted) | done |
| `expected/dv_review_report.md` | the review report of that baseline | done |
| `expected/` (rest) | known expectations used by the regression | M9 |

## Verification status (release V0.1)

Run with `semantic: {scope: all}` (live agent run, claude-sonnet-5-5, about 0.7 USD) and the released RTL as the DUT under VCS X-2025.06.
Per-test results: `reports/dv_test_results.md`; the evidence behind them is written by a run to `schemas/dv_evidence.json` and `build/sim/tb_uvm/*.log`.

| Test | Status | Note |
|---|---|---|
| TC-001 smoke | **PASS** | 11 checks, 0 mismatches |
| TC-002 INCR bursts, TC-003 FIXED, TC-004 WRAP, TC-005 narrow/strobes, TC-006 error bursts | **PASS** | 18 / 6 / 12 / 4 / 8 checks |
| TC-008 ID ordering, TC-009 max length, TC-010 random mixed, TC-012 INCR crossing 4 KB (your Q-TB-OI-006) | **PASS** | 80 / 6 / 1027 / 9 checks |
| TC-011 timing/latency | PASS, **but it does not verify timing** | its checks come from SBR-008; SBR-007 (latency) makes no check, see below |
| TC-007 backpressure | **INCOMPLETE** | ran without error, made 0 checks: ready hold-off cannot be stimulated through the VIP adapter and SBR-006 has no check. Not evidence |

Scoreboard rules with real checks: SBR-001, 002, 003, 004, 005, 008. **Not checked** (cycle-level pin properties the testbench does not observe): SBR-006
(handshake stability), SBR-007 (latency), SBR-009 (reset/idle soft checks): their requirements are not verified by this example. Functional coverage
COV-001..009 and assertions are still `todo`.

Two findings from the run (neither changes an expectation of the specification):
- TC-008 first failed with 20 mismatches: it enabled SBR-003 (read data) but not SBR-001, which maintains the reference memory SBR-003 compares against.
  The DUT returned exactly the data written. The contract's TC-008 lists no SBR-001; the test body now enables it. The contract is frozen, so this is recorded here.
- The simulator log has one `UVM_WARNING` (`DRVCONNECT`, VIP driver proxy without a sequencer), not analysed yet.

A `todo` or INCOMPLETE item is never reported as PASS (DUT-DV-023/024).

## Known limitations

Not yet: functional coverage, assertions, checks for SBR-006/007/009 (need a cycle-level pin checker), real backpressure stimulus (TC-007), stress tests.

## Run it

```bash
export AXI_VIP_HOME=<checkout of mbits-mirafra/axi4_avip @ 86b46171>/    # the existing VIP, read-only
module load synopsys/vcs/X-2025.06
cd examples/axi4_sram_ctrl && q3tui run --only tb    # contract -> scaffold -> (agent regions) -> VIP -> RTL -> VCS compile -> smoke run
```

The folder is a finished run: `src/tb/` is the generated testbench (agent regions implemented once by a live agent), `.q3tui/pipeline.json`
records what it was built from (so a re-run changes nothing and calls no LLM), `reports/` has the review, architecture and conformance reports.
`schemas/dv_evidence.json` is not committed (it holds the machine-specific run commands); a run writes it and reads the PASS from the log.
Regression: `Q3TUI_VCS_MODULE=synopsys/vcs/X-2025.06 AXI_VIP_HOME=... uv run pytest -m vcs` (the end-to-end test runs this example on a copy).

| Item | Where |
|---|---|
| spec / approved config | `spec/`, `schemas/structured_spec.json`, `schemas/final_config.json` |
| verification contract, review | `schemas/verification_contract.json`, `schemas/dv_review*.json`, `reports/dv_review_report.md` |
| TB architecture | `schemas/tb_architecture.json`, `reports/tb_architecture.md` |
| generated UVM TB | `src/tb/` |
| VIP probe and binding | `schemas/vip_integration.json` |
| RTL integration and conformance | `schemas/dut_integration.json`, `reports/dut_conformance_report.md`, `src/tb/integration/` |
| evidence | `schemas/dv_evidence.json` (written by a run) |
