---
label: "2"
title: CDC analysis
deps: [sdc]
gate: none
options:
  extra_tcl: ""   # lines of your own for the script, after the SDC (set_app_var, configure_cdc_*, create_reset …)
view: [reports/cdc_report.md, build/sanity/cdc_setup.tcl]
---
Static clock domain crossing analysis (VC Static, role `cdc`) of the RTL with the shared SDC (`constraints/sdc/sdc.tcl`). Q3TUI writes `build/sanity/cdc_setup.tcl` each run (analyze, elaborate, `read_sdc`, the resets, `check_cdc`, the report). Every fatal, error and warning is a violation, kept in `schemas/cdc_report.json` and in the table `reports/cdc_report.md` / `.csv` (No., Name, Severity, Description, Waived, Comment). A missing tool, or a run without a report, fails the step.
