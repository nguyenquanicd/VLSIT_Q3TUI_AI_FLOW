---
kind: dv_simulate
label: '5'
deps:
- tb
gate: auto
---
The UVM tests run against the released RTL: the smoke test and, with `semantic.scope: all`, every directed test the agent implemented (tool role `run_uvm`, on the simv the `tb` step built). The status of each run is read from the simulator log only: exit code 0, zero UVM_ERROR and UVM_FATAL, a PASS result line and no DV_TODO announced; a test that ran clean but made no check is INCOMPLETE, never a PASS. The **Evidence** tab lists every run that happened (`schemas/dv_evidence.json`), `reports/dv_test_results.md` the tests with the ones not run. A failure is a finding about the DUT or the testbench, never a reason to change the expected behavior.
