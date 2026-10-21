# The RTL flow (`flows/rtl/`)

RTL from a spec, checked: generate, format, style-lint, smoke-test. It stands alone (pick it with `pipeline.flow: rtl`):
its front half reuses the [spec flow](spec-flow.md)'s steps (spec, parse, config: code in `flows/spec/`) and its `rtl` step has its own generator base
(`flows/rtl/rtl/`: `gen.py`, `plan.py`, `task.py`, `rules/`), so the flow does not need the removed VLSIT tb / sva / verify / doc steps.

```text
spec ─► rules ─► parse ─► config ─► csr ─► rtl ─► format ─► style_lint ─► smoke
 human  code+LLM   human     auto    LLM+tool  LLM+tool  code     code+LLM    code+LLM      (no gate from `rules` on)
(setup)          (spec flow code)
```

| Step | Kind | What it does |
|---|---|---|
| spec, parse, config | `spec`, `vlsit_parse`, `vlsit_config` | The spec flow's steps as they are (`rules` is setup: it needs none of their results, so it runs right after `spec` and fails fast on contradicting rules) ([spec-flow.md](spec-flow.md)): requirements `REQ-NNN`, parameters, module map, questions |
| rules | `rtl.rules` | Reads the rule stack, checks it for contradictions **before any RTL is written**, writes the Verible flags (`src/rtl/style_lint/verible.flags`, `format.flags`) |
| csr | `rtl.csr` | The software-visible registers from the APB-CSR-Generator (see "CSR"): register map drafted from the spec, checked, generated, provided to `rtl` |
| rtl | `rtl.rtl` (generator base in `flows/rtl/rtl/gen.py`, rule files in `flows/rtl/rtl/rules/`: `rtl_rule.md`, `rtl_rule_default.md`) | RTL per module into `src/rtl/` (`project.rtl_dir`, `options.rtl_dir`), rule check by code, lint (role `lint`), synthesis, update-not-re-run. Follows the rule stack |
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
├── config/rtl_rules.md         YOUR rules (optional; `q3tui act rtl_rules_init` writes a skeleton)
├── config/skills/<name>/SKILL.md   YOUR skills (optional): replace or add a playbook the LLM stages may read
├── src/rtl/                    RTL (+ filelist.f, synth scripts of the rtl step) and the generated CSR module (+ its CDC synchronizer)
│   ├── csr/                    csr_map.json (the register map: yours after the first draft), csr.xlsx, debug.json, provided.json, csr_report.json
│   ├── style_lint/             verible.flags, format.flags, rules_resolved.md, rules_check.json, format.json, style_report.json
│   └── smoke/                  tb_smoke.sv, smoke_plan.md (+ .json), filelist.f, smoke_report.json
├── schemas/                    structured_spec.json, final_config.json, synth_report.json, questions/<step>.* (as in the spec flow)
├── reviews/                    parse_reviews.json, config_reviews.json (yours, as in the spec flow)
├── build/sim/                  lint / synthesis logs of the rtl step (disposable)
└── .q3tui/                     internal (work folders in .q3tui/rtl_flow/<step>/, backups in bkp/)
```

The generator's folder is `project.rtl_dir` (default `src/rtl/`; `options.rtl_dir` moves it for one step). The spec and rtl flows share the project's `pipeline.json` and `schemas/` by step name (`spec parse config`). The whole layout: [project-layout.md](project-layout.md).

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
   installing `src/rtl/<module>.sv` (+ `src/rtl/m_vlsi_synch.sv` when asynchronous) after `project.backup()`. The tool's header (user, time) is not a change.
   `src/rtl/csr/provided.json` declares them *provided*.
3. **`src/rtl/csr/csr_map.json` is yours after the first draft** (like `sdc.json`): edit it and the step generates again with no LLM; the LLM is asked only when the spec
   changed (it gets the current map and the changes, and keeps your edits).

Provided modules are in the `rtl` step's plan (the parent instantiates them and is updated when they change), in the filelist, the design lint and the synthesis;
the `rtl` step never writes, rule-checks or fixes them (a lint finding in generated code is a warning: the fix is the map's), and they are not its outputs.
`format` and `style_lint` leave them alone; `smoke` compiles the whole design but its writer cannot read them (`src/rtl/csr/` and every RTL file are denied).
Generated code is never rewritten by an LLM.

## Rules

Markdown, one rule per bullet `- **ID** text`. The stack, **first file wins**: your `config/rtl_rules.md` (when it exists), then Q3TUI's default
`rtl_rule_default.md`. The same id in both is an override. The same stack goes to three readers: the RTL generator (the `rtl` step's prompt;
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

Independent of the RTL: the writer sees the spec, the requirements and the configuration; every RTL file and `style_lint/` are in its
`deny_dirs` (the folder `src/rtl/` itself cannot be: `src/rtl/smoke/` lives in it). It works with the skill `smoke-testbench` and the tools `smoke_compile` /
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
result by code. Search order, first wins per name: `<project>/config/skills/`, `~/.q3tui/skills/`, the flow folder's `skills/`, the built-in
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
have in your `config/tools.json`. In-process MCP tools for the stages: `verible_help`, `verible_check_flags`, `verible_lint`, `smoke_compile`, `smoke_run`
(they run the roles; the runtime caps their output).

## User actions (assistant tools, `q3tui act NAME key=value`, `/act`)

`rtl_rules_init` (skeleton of `config/rtl_rules.md`), `csr_tool_install`, `csr_show` (the register map and the generated ports), `style_show` (the rule stack, the flags, the last style and smoke results), `format_now [files]`.
Settings (Settings dialog → step tabs): `csr.mode`, `csr.tool_dir`, `style_lint.max_rounds`, `smoke.max_rounds`.

## Known issues

- **KI-1: the generated CSR module is an exception to the VLSIT RTL Design Rules (REF-003).** The APB-CSR-Generator's module has its own port names
  (`i_bus_clk`, `i_bus_rstn`, `i_reg_clk`, not `i_clk_<domain>` / `i_rst_n_<domain>`), untyped `input` / `output` declarations, `localparam PARA_*_OFFSET`
  and a placeholder CDC synchronizer (`m_vlsi_synch`). It is treated as *provided*: not rule-checked, lint findings are warnings, `format` / `style_lint` skip it.
  The spec's rule is "no project-local RTL exceptions": this one is accepted for now. Not decided yet: a thin wrapper that renames the ports, or recording
  it as a decision in section 20 of the spec.
- **KI-2 (resolved): the RTL goes to `src/rtl/`, the folder every flow reads.** The generator writes `project.rtl_dir` (default `src/rtl/`), the same folder `fpga`, `asic` and `sanity` read; the working folders `csr/`, `style_lint/` and `smoke/` still live inside it, so they are in the RTL folder, not in `build/`.
- **KI-3: pending change in the CSR source (decided, not implemented).** The `csr` step still drafts the register map from the spec text with an LLM. The decision
  is: **one source of truth, the CSR workbook** (the spec holds only a pointer to it, as the VLSIT spec template requires), in the **APB-CSR-Generator's workbook
  layout**; no register is read from the spec text. The tool makes one module per sheet per run (the example workbook has two sheets, two modules): the spec says which
  sheets / modules there are and the step only generates (one run per sheet, in its own work folder); the module name of a sheet must match the module map of `parse`
  (a mismatch is handed back to `parse` as a non-blocking decision whose default is the workbook's name); the requirements of the CSR module come from `parse`'s module map; whoever may read the spec may read the workbook (the smoke writer included).
  **Where the workbook is, as the VLSIT spec template says** (section 11 of the spec): the fields *CSR workbook file* (name and location of the project copy), *Sheet used for
  this IP* (the sheet; several sheets = several modules, to be written as a list) and *Workbook revision*; the step reads them through the spec step's section / field parser (no
  `options` to point at the file, no scanning of `spec/`). A spec that has registers but leaves the file field empty (TBD) stops the step with that message.
- **KI-4: requirement ids.** The spec numbers requirements `REQ-<AREA>-<NNN>` (`REQ-REG-001`, `REQ-IF-003`, …: the VLSIT spec template), and that scheme is the one to keep.
  The VLSIT `parse` step renumbers them `REQ-NNN` (`REQ-001`, …) and links them to modules through its module map, losing the area; the pattern `REQ-NNN` is fixed in
  `structured_spec_schema.json`, `parse/step.py`, `rules_check.py` (the `// REQ-xxx` tags) and `tui/vlsit_panels.py`. Until `parse` keeps the
  spec's ids (a change of the spec flow, to agree with its owner), this flow works with `REQ-NNN`, and the requirements of the generated CSR module come from `parse`'s module map.
- **KI-5: rule policy versus REF-003.** The VLSIT spec template makes the VLSIT RTL Design Rules (REF-003) mandatory ("add no project-local RTL exceptions"). Here the project's
  `config/rtl_rules.md` wins over the default rules (same id = override) and `options.naming` can change the naming profile; the `rules` step only warns about an override. Not decided:
  add-only (an override becomes an error) or keep the override.
- **KI-6: the smoke test does not use the spec's test plan.** The spec has a requirement-based test plan (section 15.2, `TEST-xxx` with requirements and expected results). The smoke
  writer invents its own 3–6 behaviours (`S1…S6`) from the spec and the requirements, so they carry no `TEST-xxx` id. Better: pick the smoke subset of that plan and cite its ids.
- **KI-7: the generator's limits are not checked against the spec.** The APB-CSR-Generator does APB only, 32-bit data, full-strobe writes, `PPROT[1]` only. The spec says what the
  template cannot express must be recorded as a decision in section 20; the flow does not compare the spec's requirements with these limits (a spec asking for byte-masked writes
  gets a module that rejects them with SLVERR, silently).
- **KI-8: the lint configuration is not in the spec.** The spec's RTL contract (section 14.3) lists the lint rule configuration; the Verible flags of this flow live in
  `src/rtl/style_lint/` and the spec neither knows nor reads them.
- **KI-9: questions made by code have no option `impact`.** The new question convention gives every option of a design decision its impact (area, speed, power, complexity); the
  questions this flow creates itself (format of a hand-edited file, a smoke failure that keeps coming back) have options without it.

## Not in this flow

There is no verification here (testbench per requirement, SVA, mutation testing, the RTM were removed with the VLSIT flow's verification steps); the
smoke test only says the design does its basic job at all.
