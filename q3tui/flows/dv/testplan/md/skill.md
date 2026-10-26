---
kind: dv_testplan
label: '3'
deps:
- config
gate: human
---
The verification contract and the tests of the UVM testbench, built from the approved specification, the requirements and the configuration alone (never the RTL). Code drafts the contract; the `tb_intent` stage completes it (objectives, scoreboard rules, coverage goals, the tests with the requirements each checks); code validates it and freezes it by hash. What the specification leaves open is an open issue and a question for you: answer it, or accept its default (the Questions tab is where you answer; `reports/dv_review_report.md` says who decided each one). The **Tests** tab lists the tests with the requirements they check, **Requirements** which test covers each requirement. Nothing is invented for unspecified behavior. Options: `pipeline.step_options.tb.uvm_vip` (the standards and the VIP shape the contract).
