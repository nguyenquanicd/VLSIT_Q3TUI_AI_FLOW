---
name: project
title: Project settings — the project, its tools, its standard-cell libraries and its FPGA
version: 1
session: fresh
steps:
- project
- tools
- libraries
- fpga
---
The settings of the project itself, as a flow with views (no LLM, no gates): `project` (the project's settings and files), `tools` (the tool roles and modules, and whether each tool is found), `libraries` (the ASIC standard-cell libraries: technologies and corners; add one by scanning a folder), `fpga` (the FPGA devices and the one the FPGA flow uses: its template and part; the spec's part shown). Everything is saved to the project (`q3tui.yaml`, `pdks.json`, `fpgas.json`); the steps' runs only check and report.
