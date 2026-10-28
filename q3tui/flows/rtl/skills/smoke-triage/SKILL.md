---
name: smoke-triage
description: Decide whose fault a failing smoke test is (RTL, testbench, or spec) and what to ask the owner to change.
---

# Triage of a failing smoke test

You may read everything here: the spec, the requirements, the testbench, the RTL and the simulation log. Decide whose problem it is.

## Method
1. Read the failing `SMOKE_RESULT` line and the log around it; find the behaviour in the plan and the REQ ids it checks.
2. Go back to the **spec** and work out by hand what the right answer is. The spec is the reference, not the RTL and not the testbench.
3. Compare: the testbench's expected value, the RTL's behaviour, the spec's statement.
   - Spec and testbench agree, RTL differs → **RTL bug**: a change request for the module that implements it (name the module, the signal or the
     state, and the behaviour the spec states).
   - Spec and RTL agree, testbench differs → **TB bug**: say what the testbench misread (timing, a width, an expected value computed wrongly).
   - The spec is silent or contradicts itself on this point → **spec issue**: a question for the user; change nothing. Give the ways to settle it as `options` (2–4, your recommendation first), each a complete decision the user can pick as is (e.g. "Revise B16 to …", "Allow changing the generated block in place: …"), with its impact, and the `decision` it waits for (the spec's id for it, e.g. DEC-012: the failures that wait for the same one become one question).
   - You cannot tell → say so with the evidence; do not guess a side.
4. A compile error is the same triage: a port name or width mismatch between spec, testbench and RTL.

## Judgment
- Do not "fix" the testbench to match the RTL because that is quicker. A smoke test that agrees with the bug is worthless.
- Be specific: the module, the line or signal, the cycle, expected vs seen. One precise request is better than a list of hunches.
- A timeout or a hang is usually an RTL or reset problem, but check the testbench's reset and clock first.
