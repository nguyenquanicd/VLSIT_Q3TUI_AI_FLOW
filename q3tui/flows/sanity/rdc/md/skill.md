---
label: "5"
title: RDC analysis
deps: [sdc]
gate: none
options:
  extra_tcl: ""
view: [sanity/rdc_report.md, sanity/rdc_setup.tcl]
---
Static reset domain crossing analysis (VC Static, role `rdc`) of the RTL with the shared SDC. Same shape as `cdc`: `sanity/rdc_setup.tcl` (`check_rdc`), `schemas/rdc_report.json`, the table `sanity/rdc_report.md` / `.csv`.
