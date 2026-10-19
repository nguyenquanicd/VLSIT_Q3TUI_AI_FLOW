---
name: verible-flags
description: Turn the markdown RTL rules into Verible lint flags (rules, parameters) and formatter flags, and check them with the tools instead of guessing.
---

# Writing Verible flags from the rules

Goal: flags that make `verible-verilog-lint` / `verible-verilog-format` enforce what the rules say, no more, no less.

## How to work
1. Read the rules you were given (the first file wins where two conflict: it is the user's). Note which rules are about *style* (names,
   layout, structure, constructs) and which about *design meaning* (clocking, CDC, reset policy): Verible only checks the first kind.
2. For each style rule, find the Verible rule that checks it in the **catalog of the installed version** in your task (rule names and
   parameters differ between versions: never use a name that is not in it). `verible_help <rule>` gives one rule's full text; use it only when
   the catalog line does not tell you what you need, not for every rule.
3. Write the flags, then **prove them**: `verible_check_flags` runs Verible with your flags on a small sample and tells you whether it
   accepted them (a wrong rule or parameter is rejected with a message: fix that and run again). One check per attempt, on all the flags together;
   a handful of tool calls is enough. A rule that Verible accepts but does not fire is normal: do not chase it with many tests.
4. A rule Verible cannot check is not an error: list it as *not checkable* with its id. It is still the generator's and the judge's job.

## Flag format (one flag per line, no quotes)
```
--ruleset=default
--rules=+line-length=length:120,-no-tabs,+parameter-name-style=localparam_style_regex:LPARA_[A-Z0-9_]+
--rules_config_search
```
- `--ruleset=default|all|none` picks the starting set. Prefer `none` and switch on exactly the rules the rule files ask for when the rules are
  strict; use `default` when they only add to Verible's usual hygiene.
- `+rule` on, `-rule` off, `rule=param:value;param2:value2` configures: parameters of one rule are separated by `;`, rules by `,`. (Seen with Verible:
  `--rules=+line-length=length:120,+parameter-name-style=parameter_style_regex:PARA_[A-Z0-9_]+;localparam_style_regex:LPARA_[A-Z0-9_]+`.)
  Parameters have ranges (`line-length` accepts 40–1000: a value outside is a fatal error) and names (ask `verible_help <rule>`).
- **A comma inside a regex breaks the flag** (`PARA_[A-Z]{1,8}` is read as two rules): write the regex without commas (`PARA_[A-Z0-9_]+`, `[A-Z][A-Z]*`).
- Naming rules OR the built-in styles with your regex: `parameter-name-style=parameter_style_regex:PARA_[A-Z0-9_]+` alone still lets `Width`
  (CamelCase) and `WIDTH` (ALL_CAPS) pass. To enforce only your pattern, empty the style too:
  `parameter-name-style=parameter_style:;parameter_style_regex:PARA_[A-Z0-9_]+` (same for `localparam_style`). A style outside the listed choices
  is a fatal error. Always test a naming rule with `verible_check_flags`, then with `verible_lint` on a file that must fail.
- The formatter takes `--indentation_spaces=N`, `--column_limit=N`, `--wrap_spaces=N`, `--port_declarations_alignment=align` and the like.
  Use only what the rules state (indentation, maximum line length); leave the rest at its default.

## Judgment
- Two rules that cannot both hold (a line length of 80 and a name rule that forces longer names, tabs allowed here and forbidden there) are a
  *conflict*: report it, do not silently pick one. The user decides in `rules/rtl_rules.md`.
- Do not turn a rule on "to be safe". Every enabled rule must be traceable to a rule id; put the id next to it in your answer.
- Keep the flags short: one `--rules=` line is easier to review than ten.
