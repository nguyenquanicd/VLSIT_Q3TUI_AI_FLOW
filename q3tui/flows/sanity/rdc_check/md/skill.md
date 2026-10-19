---
label: "7"
title: RDC check after waivers
deps: [rdc_waivers]
gate: none
options: {}
view: [sanity/rdc_waive.tcl, schemas/rdc_check.json]
---
Code only. The `waive` decisions of `rdc_waivers` become `waive_rdc` commands (`sanity/rdc_waive.tcl`), the tool runs again with them sourced after `check_rdc`, and the violations left open must be exactly the ones the review did not waive (the count and which ones). A different count, a waived violation the tool still reports, an open one it no longer reports, or a waiver that cannot be written as a command fails the step; `schemas/rdc_check.json` has the numbers.
