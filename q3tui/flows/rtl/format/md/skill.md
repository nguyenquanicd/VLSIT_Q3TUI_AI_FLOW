---
label: '5'
title: Format (Verible)
deps:
- rtl
gate: none
options: {}
view: [rtl/style_lint/format.json, rtl/style_lint/format.flags]
---
`verible-verilog-format` on the RTL with the flags the `rules` step wrote (`rtl/style_lint/format.flags`). Whitespace only: every file is checked to
be the same code before it is written, and a backup is kept. Runs by itself after the RTL is generated or fixed. A file you edited by hand is
formatted only when you say so (a question), never silently.
