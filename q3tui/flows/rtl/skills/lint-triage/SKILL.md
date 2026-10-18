---
name: lint-triage
description: Judge Verible findings on generated RTL — real violation, wrong flag, or acceptable — and decide what goes back to the RTL generator.
---

# Triage of style findings

Verible reports findings; you decide what each means. The RTL belongs to the `rtl` step: you do not edit it. You hand it back what to fix,
as short, concrete change requests per module.

## For each finding (or group of the same rule)
1. Look at the source line and its surroundings (Read the file) and at the rule that says what is wanted (the rule ids and the flags are given).
2. Decide:
   - **violation**: the code breaks a rule → a fix for the generator.
   - **flag problem**: the flag is stricter or looser than the rule (a regex that rejects a name the rule allows, a limit that differs) → change the flags
     (you may edit the flags file you were given) and run again. Never weaken a flag just to make findings disappear: the rule text is the reference.
   - **acceptable**: the rule itself allows it (an exception in the rule text, an external protocol's name). Say why, citing the rule id.
3. Group fixes by module. One change request per module, listing each line and what to change, in the words of the rule (`rename reg_cnt to
   reg_count_0`, not "fix naming"). Include only what must change; the generator keeps the function untouched.

## Judgment
- Many findings of one rule usually mean one cause (a wrong regex, a missing template): fix that once instead of listing every line.
- Never ask for a change that alters behaviour; if the only way to satisfy a rule is a functional change, say so as a question for the user.
- A syntax error is not a style finding: report it as a failure of the file; the generator fixes it.
- After you changed flags, run the lint again; finish with the findings you stand behind, not with the first run.

## Output
`fixes`: per module the change request text. `waived`: findings you accept, each with the rule id that allows it. `flag_changes`: what you
changed in the flags and why. `clean` is true only when nothing remains to fix.
