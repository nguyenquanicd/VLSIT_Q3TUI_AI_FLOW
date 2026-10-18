# The RTL flow (`flows/rtl/`)

RTL from a spec, checked and ready for the verification flow: generate, format, style-lint, smoke-test. It is its own flow, independent of
`vlsit` (pick one with `pipeline.flow: rtl`); it reuses the VLSIT steps for the front half and adds four steps of its own.

```text
spec ─► rules ─► parse ─► config ─► csr ─► rtl ─► format ─► style_lint ─► smoke
 human  code+LLM   human     auto    LLM+tool vlsit   code     code+LLM    code+LLM      (no gate from `rules` on)
(setup)          (vlsit)  (vlsit)         (vlsit)
```

| Step | Kind | What it does |
|---|---|---|
| spec, parse, config | `spec`, `vlsit_parse`, `vlsit_config` | The VLSIT steps as they are (`rules` is setup: it needs none of their results, so it runs right after `spec` and fails fast on contradicting rules) ([vlsit-flow.md](vlsit-flow.md)): requirements `REQ-NNN`, parameters, module map, questions |
| rules | `rtl.rules` | Reads the rule stack, checks it for contradictions **before any RTL is written**, writes the Verible flags (`rtl/style_lint/verible.flags`, `format.flags`) |
| csr | `rtl.csr` | The software-visible registers from the APB-CSR-Generator (see "CSR"): register map drafted from the spec, checked, generated, provided to `rtl` |
| rtl | `rtl.rtl` (the VLSIT `rtl` step) | RTL per module into `rtl/` (`options.rtl_dir`), rule check by code, lint (role `lint`), synthesis, update-not-re-run. Follows the rule stack |
| format | `rtl.format` | `verible-verilog-format` with `format.flags`. Code only |
| style_lint | `rtl.style_lint` | Verible lint with `verible.flags`, judged by an LLM; fixes go back to their owners |
| smoke | `rtl.smoke` | A basic-function testbench written from the spec alone, compiled with the RTL and run |

The chain runs by itself: `rtl → format → style_lint`, and when `style_lint` finds something to fix it hands it back to `rtl` (scoped change request
`[rtl:<module>] …`), which updates that module only, then `format` and `style_lint` run again; then `smoke`, whose failures go back the same way. The
engine's fix loop does this (`meta["loop"]`, as `verify`), bounded by `options.max_rounds` of `style_lint` and of `smoke` (else
`pipeline.max_fix_iterations`). A bound reached fails the step: leftovers are never passed.

## Layout

```text
<project>/
├── spec/                       spec.md (input)
├── rules/rtl_rules.md          YOUR rules (optional; `q3tui act rtl_rules_init` writes a skeleton)
├── skills/<name>/SKILL.md      YOUR skills (optional): replace or add a playbook the LLM stages may read
├── rtl/                        RTL (+ filelist.f, synth scripts of the rtl step) and the generated CSR module (+ its CDC synchronizer)
│   ├── csr/                    csr_map.json (the register map: yours after the first draft), csr.xlsx, debug.json, provided.json, csr_report.json
│   ├── style_lint/             verible.flags, format.flags, rules_resolved.md, rules_check.json, format.json, style_report.json
│   └── smoke/                  tb_smoke.sv, smoke_plan.md (+ .json), filelist.f, smoke_report.json
├── schemas/                    structured_spec.json, final_config.json, synth_report.json, questions/<step>.* (as in vlsit)
└── .q3tui/                     internal (work folders in .q3tui/rtl_flow/<step>/, backups in bkp/)
```

The `vlsit` flow keeps its own layout (`src/rtl/`). The two flows share the project's `pipeline.json` and `schemas/` by step name (`spec parse config
rtl`): use one flow per project.

## CSR (the registers are generated, not written)

[APB-CSR-Generator](https://github.com/nguyenquanicd/APB-CSR-Generator) turns an Excel register map into an APB4 slave (address decode, SLVERR / PREADY,
`rw` / `ro` / `rwi` / `w1c` fields, an optional clock-domain-crossing bridge). Q3TUI drives it; it is not shipped (the repository has no license):
`q3tui act csr_tool_install` clones it to `~/.q3tui/tools/APB-CSR-Generator`; also found by `options.tool_dir`, `$CSR_GEN_HOME`, `<project>/tools/…`. Role `csr_gen`
(`{python} {tool}/CSR_Generation.py {workbook} {sheet}`, Q3TUI's interpreter: it has `openpyxl`).

`csr` (mode `auto`: only when the spec says register / CSR / SFR / memory-mapped, else no LLM and nothing generated; `on` always; `off` never):

1. **LLM, skills `csr-register-map` and `apb-csr-tool`, tool `csr_check`**: drafts the register map from the spec (module name from the module map, registers,
   offsets, fields, access types, resets, asynchronous or not, the REQs). `csr_check` validates a map and, with the generator installed, really generates it in
   a scratch folder and lists the ports it will have. Questions (`spec_gap`) for what the spec leaves open.
2. **Code**: `reserved` filler for uncovered bits, the checks the tool does not do (duplicate / misaligned offsets, overlapping or out-of-range fields, a reset
   too wide, identifiers, 32-bit data only; a map with problems goes back to the LLM, 3 tries), the workbook in the layout the tool reads, running the tool, and
   installing `rtl/<module>.sv` (+ `rtl/m_vlsi_synch.sv` when asynchronous) after `project.backup()`. The tool's header (user, time) is not a change.
   `rtl/csr/provided.json` declares them *provided*.
3. **`rtl/csr/csr_map.json` is yours after the first draft** (like `sdc.json`): edit it and the step generates again with no LLM; the LLM is asked only when the spec
   changed (it gets the current map and the changes, and keeps your edits).

Provided modules are in the `rtl` step's plan (the parent instantiates them and is updated when they change), in the filelist, the design lint and the synthesis;
the `rtl` step never writes, rule-checks or fixes them (a lint finding in generated code is a warning: the fix is the map's), and they are not its outputs.
`format` and `style_lint` leave them alone; `smoke` compiles the whole design but its writer cannot read them (`rtl/csr/` and every RTL file are denied).
Generated code is never rewritten by an LLM.

## Rules

Markdown, one rule per bullet `- **ID** text`. The stack, **first file wins**: your `rules/rtl_rules.md` (when it exists), then Q3TUI's default
`vlsit_rtl_rule_default.md`. The same id in both is an override. The same stack goes to three readers: the RTL generator (the `rtl` step's prompt;
your file is also one of its inputs, so editing it updates the modules), the `rules` step (→ flags) and the style judge. Q3TUI's own naming check
(`rules_check.py`) is a profile by code; a rule that changes a name pattern is half applied unless `options.naming` of `rtl` says the same: the
`rules` step warns about it.

`rules`: one LLM stage, skills `verible-flags` and `rule-consistency`, tools `verible_help` / `verible_check_flags`. It answers with findings,
the lint flags, the format flags, the rule→Verible mapping and the rules Verible cannot check. An `error` finding (a contradiction) fails the step
with the exact text to change in your file; `warning`s are logged. Code: the rule stack and its diff by rule id (nothing changed → no LLM; a change
→ the task names the changed rules and gives the flags as they are), the flags' shape check (`--rules` / `--ruleset` / `--rules_config` /
`--waiver_files` only, no rule both on and off) with up to two repair rounds, and writing the files.

## format

Whitespace only, enforced: each file is formatted as a copy, written back only when it is the same code once whitespace is dropped, after
`project.backup()`. A file with a syntax error is left to `rtl` and `style_lint`. A file you edited by hand (its hash is not what the `rtl` step
recorded) is never touched silently: a blocking question asks first. The formatted files are recorded as the `rtl` step's outputs again, so a format
is not mistaken for your edit (your edited files stay flagged as yours). `q3tui act format_now [files=a,b]` formats on request.

## style_lint

Code runs Verible (the findings are ground truth). No finding: no LLM. Otherwise one judge stage (skill `lint-triage`; Read / Grep / Glob, the tools
`verible_lint` to test a hunch) answers per module: `fixes`, `flag_problems`, `waived` (accepted, with the rule that allows it), `questions`. Code
dispatches: a fix → `rtl` as `[rtl:<module>] …`, a flag problem → `rules`, a waiver is remembered (the next run does not judge it again), a syntax
error goes to `rtl` without the LLM. The judge edits neither the RTL (the `rtl` step's) nor the flags (the `rules` step's).

## smoke

Independent like the VLSIT `tb`: the writer sees the spec, the requirements and the configuration; every RTL file and `style_lint/` are in its
`deny_dirs` (the folder `rtl/` itself cannot be: `rtl/smoke/` lives in it). It works with the skill `smoke-testbench` and the tools `smoke_compile` /
`smoke_run` (roles `sim` and `run`), so it iterates by itself; a compile error about a name means its reading of the spec's interface differs from the
design: it fixes its side or asks a `spec_gap` question, it never adapts to the RTL. Result lines: `SMOKE_RESULT <id> PASS|FAIL …`,
`SMOKE_SUMMARY total=<n> failed=<n>`; no line, a timeout or a mismatching summary is a failure. **Code decides the result**: it compiles and runs
again after the writer. On a failure a second LLM (skill `smoke-triage`, may read everything) gives a verdict per failing behaviour: `rtl_bug` →
`[rtl:<module>] …` to `rtl`; `tb_bug` → a change request to `smoke`; `spec_issue` / `unsure` → a blocking question (kind `spec_gap` / `design_choice`).
Update, not re-run: the testbench is rewritten only when the spec, the requirements, the parameters or your answers changed (the task says what);
a changed RTL only compiles and runs again. While a fix handed to `rtl` / `rules` is pending the step waits (`missing_input`), so it never looks at
RTL that is about to change.

## Skills

A skill is `<name>/SKILL.md` (front matter `name`, `description`; the body is the playbook). The stage's prompt lists them with one line each; the LLM
reads the ones the task needs and decides how to use them. Q3TUI scripts nothing inside a skill: it gives tools, confines files and checks the
result by code. Search order, first wins per name: `<project>/skills/`, `~/.q3tui/skills/`, the flow folder's `skills/`, the built-in
`flows/rtl/skills/`: so a project replaces or adds a playbook by dropping a folder. Built-in: `csr-register-map`, `apb-csr-tool`, `verible-flags`, `rule-consistency`,
`lint-triage`, `smoke-testbench`, `smoke-triage`. Skills are not part of a step's staleness (like prompts, they apply to the next LLM task).

## Tools (`flows/rtl/tools.json`, by role)

| Role | Used by | Default |
|---|---|---|
| `verible_lint` | rules, style_lint | `verible-verilog-lint {args} {files}`, parse `verible`, exit code 1 (findings) is a result |
| `verible_format` | format | `verible-verilog-format {args} {files}` (`--inplace` added by code) |
| `lint`, `synth` | rtl | the project's tool set (the built-in Synopsys preset when it has none): VCS lint, Design Compiler |
| `csr_gen` | csr | `{python} {tool}/CSR_Generation.py {workbook} {sheet}` |
| `sim`, `run` | smoke | the same: VCS compile, `simv` |

The flow's `tools.json` only fills a role nothing else configures. A missing tool makes its step wait (`missing_input`) with a message naming the
role: a check whose tool is missing is not passed. There is no fallback between tools; configure `sim` / `run` for Verilator, Icarus or whatever you
have in your `tools.json`. In-process MCP tools for the stages: `verible_help`, `verible_check_flags`, `verible_lint`, `smoke_compile`, `smoke_run`
(they run the roles; the runtime caps their output).

## User actions (assistant tools, `q3tui act NAME key=value`, `/act`)

`rtl_rules_init` (skeleton of `rules/rtl_rules.md`), `csr_tool_install`, `csr_show` (the register map and the generated ports), `style_show` (the rule stack, the flags, the last style and smoke results), `format_now [files]`.
Settings (Settings dialog → step tabs): `csr.mode`, `csr.tool_dir`, `style_lint.max_rounds`, `smoke.max_rounds`.

## Not in this flow

Verification (testbench per requirement, SVA, mutation testing, the RTM) is the verification flow's job; the smoke test only says the design does its
basic job at all. The verification flow reading `rtl/` instead of `src/rtl/` is not wired yet.
