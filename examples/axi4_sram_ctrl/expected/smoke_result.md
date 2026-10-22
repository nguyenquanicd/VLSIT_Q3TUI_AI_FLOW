# Smoke result of axi4_sram_ctrl (reference copy of a real run)

Taken from `schemas/dv_evidence.json` and the simulator log of a run through the CLI (`q3tui run --only tb`, project `examples/axi4_sram_ctrl`).
Machine-specific paths are removed. The proof is your own run: it writes `schemas/dv_evidence.json` and `.q3tui/sim/tb_uvm/*.log` in the project.

| Entry | What | Return code | UVM_ERROR | UVM_FATAL | Status |
|---|---|---|---|---|---|
| compile-001 | testbench + VIP + interface shell (no DUT): syntax and integration check, not a DUT result | 0 | n/a | n/a | PASS |
| compile-002 | testbench + VIP + released RTL (DUT): compile and elaboration | 0 | n/a | n/a | PASS |
| run-003 | `dut_smoke_test`, seed 1, released RTL as DUT | 0 | 0 | 0 | PASS |

Result line in the log:
```
VLSIT_DUT_TB_RESULT test=dut_smoke_test tc=TC-001 status=PASS checks=11 mismatches=0 todo=0
TC_RESULT TC-001 PASS
```

Command (portable form): `vcs -full64 -sverilog -ntb_opts uvm-ieee-2020-2.0 -timescale=1ns/1ps -suppress=ETMFCB -debug_access+all -top tb_top -f <filelist> ...`,
then `simv +UVM_TESTNAME=dut_smoke_test +ntb_random_seed=1 +UVM_NO_RELNOTES`. Tool: Synopsys VCS X-2025.06.

Known warning in this run (1 UVM_WARNING, not an error): `DRVCONNECT` from the VIP master driver proxy: its `seq_item_port` has no sequencer connected.
Its cause and whether it matters for stimulus have not been analysed yet.

Not proven by this run: TC-002..TC-012, scoreboard rules SBR-004..007 and SBR-009, functional coverage, assertions (all `todo`, see the README).
