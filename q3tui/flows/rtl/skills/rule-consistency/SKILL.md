---
name: rule-consistency
description: Check the stack of RTL rules (the user's config/rtl_rules.md over Q3TUI's defaults) for conflicts, duplicates and rules the tools cannot honour.
---

# Consistency of the rule stack

The rules reach three readers: the RTL generator, Verible (as flags) and the style judge. A contradiction becomes a loop of
generate → violate → fix. Find them *before* RTL is written.

## What to look for
- **Same id twice** inside one file: an error. The same id in the user's file and in the default one is an *override*: the user's wins; say
  which rules are overridden so the user sees the effect.
- **Direct contradictions**: one rule requires what another forbids (a prefix, a reset style, a construct, a limit). Name both ids and the
  case where they collide.
- **Limits that cannot all hold**: a maximum name length shorter than what the naming table builds (prefix + function + suffix), a line
  limit smaller than the header template, an indentation the formatter cannot produce.
- **Overlap with the code-enforced checks**: Q3TUI checks naming by regex (the profile it names in your task). A user rule that changes a
  name pattern is only half applied unless the profile option `naming` is set too: report it as a warning and say which pattern.
- **Rules nobody can check** (vague words: "clean", "reasonable"): a warning, with a way to make it checkable.
- **Out-of-scope rules** (testbench rules in a design rule file).

## Severity
- `error`: the stack cannot be followed as written (the flow stops until the user edits `config/rtl_rules.md`). Use it for real
  contradictions only.
- `warning`: it can be followed, but something surprising happens (an override, a half-applied naming rule, a vague rule).

## Output
For each finding give: the ids involved, what collides, a concrete suggestion for the user's file (the exact text to change), severity.
No findings is a valid answer; do not invent some.
