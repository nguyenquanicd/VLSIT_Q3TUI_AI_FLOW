# Directed test results (released RTL as the DUT)

Status is read from the simulator log of each run (`schemas/dv_evidence.json`); `todo` = not implemented, not run; `INCOMPLETE` = ran without error but made no check.

| Test | Class | Status | Note |
|---|---|---|---|
| TC-002 | `dut_tc_002_incr_bursts_test` | PASS | TC-002 with the released RTL as the DUT |
| TC-003 | `dut_tc_003_fixed_bursts_test` | PASS | TC-003 with the released RTL as the DUT |
| TC-004 | `dut_tc_004_wrap_bursts_test` | PASS | TC-004 with the released RTL as the DUT |
| TC-005 | `dut_tc_005_narrow_and_strobes_test` | PASS | TC-005 with the released RTL as the DUT |
| TC-006 | `dut_tc_006_error_bursts_test` | PASS | TC-006 with the released RTL as the DUT |
| TC-007 | `dut_tc_007_backpressure_test` | INCOMPLETE | result line says INCOMPLETE (todo=0, checks=0, mismatches=0) |
| TC-008 | `dut_tc_008_id_ordering_test` | PASS | TC-008 with the released RTL as the DUT |
| TC-009 | `dut_tc_009_max_length_bursts_test` | PASS | TC-009 with the released RTL as the DUT |
| TC-010 | `dut_tc_010_random_mixed_test` | PASS | TC-010 with the released RTL as the DUT |
| TC-011 | `dut_tc_011_timing_latency_test` | PASS | TC-011 with the released RTL as the DUT |
| TC-012 | `dut_tc_012_incr_cross_4kb_test` | PASS | TC-012 with the released RTL as the DUT |
