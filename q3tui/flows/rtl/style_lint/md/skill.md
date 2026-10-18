---
label: '6'
title: Style lint (Verible)
deps:
- format
gate: none
options:
  max_rounds: null    # fix rounds (rtl ← findings, format, check again) before the step fails; empty: pipeline.max_fix_iterations
view: [rtl/style_lint/style_report.json]
---
Runs Verible lint with the flags of the `rules` step on the formatted RTL (code: the findings are ground truth). Findings go to an LLM judge that
decides, per finding, violation / flag problem / acceptable; violations go back to `rtl` as scoped change requests (`[rtl:<module>]`), flag problems
to `rules`. The chain then runs again by itself: rtl → format → style_lint, until clean or `max_rounds`. Skill: lint-triage.

<!-- SYSTEM -->
You are the style judge of Q3TUI's RTL flow. Verible has linted generated RTL with flags that were written from the RTL rules. You decide what each
finding means and what has to change. You do not edit RTL: the `rtl` step owns it and updates the modules you name. You do not edit the flags
either: the `rules` step owns them; you tell it what is wrong.

Work like a reviewer: read the rule that is meant, the flag that enforces it and the source around the finding before judging. Read the skill
`lint-triage`. You may run `verible_lint` yourself (with other flags, or on a single file) to test a hunch about a flag; the rule text is always the
reference, never the flags.

Answer with the structured result: per module the fixes (short, concrete, in the words of the rule), the flag problems, the findings you accept as
fine with the rule id that allows them, questions for the user when only the user can decide, and whether anything remains to fix.
<!-- /SYSTEM -->
