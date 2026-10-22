---
label: "5"
title: RDC analysis
deps: [sdc]
gate: none
options:
  extra_tcl: ""
view: [reports/rdc_report.md, build/sanity/rdc_setup.tcl]
---
Static reset domain crossing analysis (VC Static, role `rdc`) of the RTL with the shared SDC. Same shape as `cdc`: `build/sanity/rdc_setup.tcl` (`check_rdc`), `schemas/rdc_report.json`, the table `reports/rdc_report.md` / `.csv`.
