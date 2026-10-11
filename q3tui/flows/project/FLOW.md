---
name: project
title: Project settings — the project, its tools and its standard-cell libraries
version: 1
session: fresh
steps:
- project
- tools
- libraries
---
The settings of the project itself, as a flow with views (no LLM, no gates): `project` (the project's settings and files), `tools` (the tool roles and modules, and whether each tool is found), `libraries` (the ASIC standard-cell libraries: technologies and corners; add one by scanning a folder). Everything is saved to the project (`q3tui.yaml`, `pdks.json`); the steps' runs only check and report.
