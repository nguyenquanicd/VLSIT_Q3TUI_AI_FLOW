---
name: example
title: Minimal folder flow (copy this folder to <project>/flows/ and edit)
session: fresh
steps: [spec, parse]
---
A flow is a folder: this file (flow settings + the order of `steps`) and one folder per step:

    <step>/md/skill.md      front matter (kind, deps, gate, options, view, pass) + the skill: notes for the step's LLM tasks,
                            and `<!-- NAME -->` … `<!-- /NAME -->` sections = stage system prompts of the step's code
    prompts/<step>.md       optional: system-prompt override (`mode: append | replace`)
    <step>/step.py          optional: class `Step(StepDef)` — the step's own code (used when skill.md names no `kind`)
    <step>/rules/           optional: rule files
    <step>/schemas/         optional: JSON schemas
    <step>/scripts/         optional: helper scripts the step runs
