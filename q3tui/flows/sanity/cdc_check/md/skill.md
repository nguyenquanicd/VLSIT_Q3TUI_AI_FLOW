---
label: "4"
title: CDC check after waivers
deps: [cdc_waivers]
gate: none
options: {}
view: [sanity/cdc_waive.tcl, schemas/cdc_check.json]
---
Code only. The `waive` decisions of `cdc_waivers` become `waive_cdc` commands (`sanity/cdc_waive.tcl`), the tool runs again with them sourced after `check_cdc`, and the violations left open must be exactly the ones the review did not waive (the count and which ones). A different count, a waived violation the tool still reports, an open one it no longer reports, or a waiver that cannot be written as a command fails the step; `schemas/cdc_check.json` has the numbers.
