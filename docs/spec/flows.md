# Flows — pipelines as data, tools by role, the picker

Q3TUI's pipeline is a **flow**: a YAML / JSON file listing the steps (by *kind*), their order and dependencies and
which review gates stop for a human. The built-in flows are the **spec flow** (`spec`, the default;
docs/spec/spec-flow.md: spec → parse → config), the **FPGA flow** (`fpga`, docs/spec/fpga-flow.md), the **ASIC flow** (`asic`, docs/spec/asic-flow.md), the **Sanity flow** (`sanity`, docs/spec/sanity-flow.md), the **project settings flow** (`project`, docs/spec/project-flow.md), the **RTL flow** (`rtl`, docs/spec/rtl-flow.md: rules → Verible flags, RTL, CSR from a generator, format, style lint, smoke test) and `example` (a minimal folder flow, spec → parse, to copy); users can write their own. The TUI,
`ops`, the assistant, the event bus, staleness, gates, questions and reset are the same for every flow.

## Flow files

Found by name in `<project>/flows/`, `~/.q3tui/flows/` (`$Q3TUI_HOME`), then the built-in
`q3tui/flows/` (first hit wins), or by path. Selected with `pipeline.flow: <name | path>` or `q3tui --flow`. The default is `spec`.

```yaml
name: spec
title: Spec flow
version: 1
gap_targets: {spec_gap: spec, req_gap: parse}   # question kind -> the step that owns the answer (built in: spec_gap -> spec)
sees: {smoke: [spec, parse, config]}            # step -> steps whose settled decisions it may receive (independence)
tools: [lint, synth, sim]    # roles the flow needs (besides those its step kinds declare)
steps:
  - {id: spec,  kind: spec,        label: "0", gate: human}
  - {id: parse, kind: vlsit_parse, label: "1", gate: human, deps: [spec]}
  - id: rtl
    kind: rtl_gen     # (a kind registered by a flow or an extension; the rtl flow's own is `rtl.rtl`, a step.py)
    deps: [config]
    options: {rules: [rtl_rule.md, rtl_rule_default.md], lint_max_warnings: 0}
```

| Key (step) | Meaning |
|---|---|
| `id` | Name of the step in this flow (state, CLI, TUI). Default: the kind. |
| `kind` | A registered step kind (`q3tui.steps.KINDS`; `register_kind` adds one). |
| `deps` | Steps it depends on (reset cascades, execution order). Default: the kind's own. |
| `gate` | `human` · `auto` · `auto_answer` · `none`/absent. |
| `options` | Kind-specific settings (prompts, rule files, limits). Part of the step's staleness: changing them makes it stale. |
| `label`, `title` | Short tag shown before the name, and its title. |

Steps run in dependency order (file order where free). `q3tui flow list | check [flow]` lists flows and validates one:
duplicate ids, unknown kinds / deps / gap targets, cycles, and the tool roles it needs.

A flow combines existing kinds and sets their gates, options, prompts and tools. A genuinely new kind of work is a
Python `StepDef` registered with `register_kind` — a flow file is not a scripting layer.

Kinds: `spec` (q3tui/steps/spec) and, for the spec flow, `vlsit_parse vlsit_config` (q3tui/flows/spec/*, docs/spec/spec-flow.md); the rtl flow's steps are its own `step.py` kinds (`rtl.rtl`, …).

## Flow folders

A flow can be a **folder** `<name>/` (same search path; built-in: `q3tui/flows/spec/`, template: `flows/example/`),
one folder per step:

```
<name>/FLOW.md                front matter: name, title, session, tools, sees, gap_targets, steps: [ids in order]; body: description
<name>/<step>/md/skill.md     the step's skill: front matter (kind, label, deps, gate, options, view, pass); body: named
                              `<!-- NAME -->` sections = its stage system prompts, the rest = notes added to every LLM task
<name>/<step>/task.py         assembles the per-task input (data → text); no prompt text lives in code
<name>/prompts/<id>.md        optional: system-prompt override of a step / stage (mode: append | replace), see Prompts
<name>/<step>/step.py         the step's code: class `Step(StepDef)` (a kind of this flow when skill.md names no `kind`;
                              the spec flow's parse and config steps are registered kinds, their step.py holds the real code)
<name>/<step>/rules/          optional: rule files (options.rules)
<name>/<step>/schemas/        optional: JSON schemas
<name>/<step>/scripts/        optional: helper scripts the step runs
```

The built-in `flows/` directory is a Python package (`q3tui.flows`, the flow loader in `__init__.py`); `flows/spec/<step>/`
holds each spec step's `step.py` and its private modules (`task.py`, `check.py`, `plan.py`, `gen.py`…). Helpers shared by several
steps stay in `q3tui/flows/spec/lib/` (`base`, `artifacts`, `constraints`; the rtl flow imports them) and, for the rtl flow, in `q3tui/flows/rtl/lib/` (`rules_check`, `synth`, …).

| Step key | Meaning |
|---|---|
| `view: [globs]` | extra project files the step's Files tab shows |
| `pass: [conditions]` | checked when the step finished; unmet → it fails: `exists <glob>`, `contains <file> <regex>`, `run <cmd>` (exit 0) |
| body, outside `<!-- NAME -->` sections (notes) | appended to every LLM task of the step ("Flow notes for this step") |

Notes and pass conditions are part of the step's staleness. `view` / `pass` / `notes` also work in YAML / JSON / single-file
markdown flows (`<name>.md`: `## <step id>` sections with `- key: value` lines, then the notes). To customise a built-in
flow, copy its folder to `<project>/flows/` (it wins by name). Overrides without touching the flow: `pipeline.step_flow:
{rtl: {pass: [...], notes: "..."}}` in `q3tui.yaml` or `/step`, through `Ops.set_step_setting`.

Prompts: a step's `<!-- NAME -->` sections can be replaced or extended by `<flow folder>/prompts/<step or stage>.md` (see "Flow editor and prompts").

**What a flow can say in markdown, and what needs code.** Markdown (`FLOW.md`, `skill.md`, single-file flows): everything in the tables
above, the prompts, notes, `view`, `pass`, `options`, `gate`. Code in the flow folder: the step's `StepDef` hooks in `step.py` and the
add-on files.

| `StepDef` hook (`<step>/step.py`) | When |
|---|---|
| `inputs` / `outputs` / `missing_input` / `run` | what it reads, writes, needs, does (staleness is the hash of the inputs) |
| `on_approve(engine)` | its gate was approved (a gate that approves itself also calls it) |
| `follow_up(engine)` | right after a human approval: `{title, detail, action, switch}` — the TUI asks, yes runs the flow's action and moves to flow `switch` (fpga: hand the PPA fixes to rtl) |
| `unattended(engine)` (async) | an unattended run (`auto_answer`) passed this step's gate: do what a person would do next; True runs the flow again from its start (bounded by the step's own limits) |
| `on_reset` / `on_rebase` / `reset_files` / `refresh_reports` | after a reset, when the flow's files moved, what a reset deletes, reports re-derived from outputs |
| `pending_reviews` / `open_questions` / `default_to_apply` | the step's reviewable items and questions |

## Add-ons

Core (CLI, TUI, assistant, `Ops`, engine) knows no flow by name. A flow *folder* may bring files next to `FLOW.md`
(`q3tui/addons.py`):

| File | Brings |
|---|---|
| `addon.py` | `register(api)`: **actions** (`@api.action(name, help, {param: description}, optional=(…))` on `fn(ops, **params) -> str`; all params strings; a `ValueError` is a user error) step kinds (`api.kind`) and step options for the Settings dialog (`api.setting(step, key, label, kind, help, choices, nullable, default)`: the step's own tab, the assistant's get/set_settings; applied to the live step, saved to `pipeline.step_options.<step>.<key>` in q3tui.yaml; `step` may be another flow's step — a choice both flows share, e.g. the ASIC library from the FPGA flow — and `default` is its value while q3tui.yaml sets none) |
| `panels.py` | TUI panels of the flow's step kinds (`register_panel`); imported when the TUI opens the flow |

Every action reaches the user three ways, all through `Ops.run_action`: an assistant tool per action, `q3tui act NAME key=value …`
(no name: list them) and the TUI's `/act NAME key=value …`. A panel calls the same action for its keys. An action of the
flow must not be needed by core: the FPGA flow's SDC editor (docs/spec/fpga-flow.md) is the example.

## Playbook skills (`skills/<name>/SKILL.md`)

Not to be confused with a step's `md/skill.md` (its prompts and settings). A flow folder may bring `skills/<name>/SKILL.md` playbooks (front matter
`name`, `description`): an LLM stage's prompt lists them and the LLM reads the ones it needs, using its own judgment — Q3TUI gives the tools and
checks the result, it does not script the steps. A project adds or replaces one with `<project>/config/skills/<name>/SKILL.md` (also `~/.q3tui/skills/`).
`flows/rtl/lib/skills.py` is the loader; the RTL flow uses them (docs/spec/rtl-flow.md "Skills").

## Skill editor (TUI)

`K` / `/skill` (assistant `get_skill` / `set_skill`; `Ops.skill_get` / `skill_set`): pick a step of a flow folder and edit its whole
`md/skill.md` — settings (front matter), notes, and the `<!-- NAME -->` sections that are its stage system prompts. A built-in
flow is copied to `<project>/flows/<name>/` on the first save (the copy wins by name; the code stays in the package). Notes, `view`
and `pass` apply at once; changed prompt sections apply to the next LLM task (see `prompts.skill_overrides`); `kind`, `deps`,
`gate` and `options` after a restart.

## Flow editor and prompts (TUI)

`F` / `/flow`: the **flow editor** — pick a step, move it (↑ ↓), remove it, add one (id + kind, after the current), and edit its
dependencies, gate, `view`, `pass` and notes. Settings of existing steps are saved as `pipeline.step_flow` / `gate_modes`
(effective at once); structural changes (add / remove / move / deps) are written to the flow file — a project flow keeps its format,
a built-in or user flow is forked to `<project>/flows/<name>.yaml` (which wins) — via `Ops.save_flow_steps`, and apply after a restart.

`P` / `/prompts` (assistant `get_prompt` / `set_prompt`): the **prompt editor**. Every LLM stage's system prompt can be extended or
replaced by a markdown file `<project>/prompts/<step id or stage name>.md` (also `<flow folder>/prompts/`, and `~/.q3tui/prompts/` for all projects; front matter
`mode: append|replace`, default append; the step's file applies to all its stages, then the stage's own). The built-in prompt of each stage
is recorded in `.q3tui/prompts/<step>.md` when it runs, and shown read-only next to the override. Applied in `StepContext.llm`
(`q3tui/llm/prompts.py`), so it works the same in fresh, team and flow sessions. Not covered: the team roles' charters (`llm/roles.py`)
and the RTL rule files (those are `options.rules` of the rtl step: a project path wins over the shipped one).

## Sessions

`session:` in the flow file (or `pipeline.session`): `fresh` (the default when a flow names none; every LLM stage its own
session, where team mode may group them by role, docs/spec/team.md) or `flow`: **one persistent Claude session for the whole flow**, like the original
VLSIT's single Claude Code conversation (no built-in flow uses it now; set `session: flow` in a flow file or `pipeline.session`). In `flow` mode every stage of every step is a task (`# Task: <stage>`) in that
session (role `flow`, `llm/roles.py`; kept in `.q3tui/team/roles.json`, notes in `schemas/NOTES.md`, `q3tui team reset`
starts over), tasks run one at a time, and Claude compacts the session itself when it grows. Reviewers (stages named
`*review*`, `roles.FLOW_FRESH`, e.g. `spec_review`) still get a fresh session: fresh eyes, and the flow's session does not
grow by the whole document they read. `fresh_steps: [<step ids>]` in the flow file runs every stage of those steps fresh
too (a spec-flow copy with `session: flow` would list `spec`, `parse`, `config` — their prompts carry the spec, answers and settled decisions, so the shared session
only grew: 950k tokens and a compaction by the parse step); `q3tui flow check` reports an unknown id. Each task still carries its own
file confinement (`deny_dirs` / `write_files`), but the session remembers what it saw — a smoke testbench written in the flow
session is no longer independent of the RTL (that is the original's behaviour; use `session: fresh` for independence).

## Gate modes

Each gate has a mode; the flow file sets the default, `pipeline.gate_modes: {step: mode}` overrides it,
`pipeline.gates` (when set) says which steps have a gate at all.

| Mode | Behaviour |
|---|---|
| `human` | stops for your review (approve / answer questions / edit / request changes / later) |
| `auto` | approves itself when the step is done; open *blocking* questions still stop it (non-blocking take their defaults) |
| `auto_answer` | unattended: approves, every question takes its default (proposals accepted, bounded by `pipeline.max_fix_iterations`) |
| `none` | no review gate (blocking questions still stop the pipeline) |

`pipeline.auto_approve` / `pipeline.auto_answer` (and `--auto-approve`, `--auto-answer`, `--yes`) stay as project-wide
switches on top. Change a mode: `q3tui gate STEP MODE`, TUI `/gate STEP MODE` (or `g` on a step), assistant
`set_gate_mode`, Settings → Gates. All go through `Ops.set_gate_mode`.

## Tools by role

**One place:** the project's `config/tools.json` is the only source of tools; users write it. No flow brings defaults and there is no built-in
fallback: a role it does not define is *not configured*, and a step that needs it waits (`missing_input`) saying so. `q3tui tools init` writes a
starting file with the roles of every flow (the Synopsys preset `q3tui/eda/presets/synopsys.json` for lint / synth / sim / run, plus
`presets/flows/*.json`: Verible, the CSR generator, Vivado / Yosys / nextpnr, Design Compiler / Formality, VC Static) to edit for your site.
`q3tui tools check` and the Tools tab of the project flow list every role the flows need, found or not (`not configured` when the file lacks it).
LLM stages that may run a tool get the in-process `mcp__q3tui__tools_available`, which names each role as available or not, and says
"No tools are available" when none can run.

The tools file (`tools.json` / `tools.yaml` in the project's `config/`, or `tools.file`) — or `tools.roles:` in
`q3tui.yaml` — maps a *role* to a command; steps call roles, never binaries:

```json
{
  "setup_script": "sourceme.sh",
  "modules": [],
  "roles": {
    "lint":  {"cmd": "vcs -full64 -sverilog +lint=all -top {top} -f {filelist} -Mdir={workdir}/csrc_lint_{top} -o {workdir}/simv_lint_{top}", "parse": "vcs",
              "description": "RTL lint; 0 warnings required"},
    "synth": {"cmd": "dc_shell -f {script}", "parse": "dc"},
    "sim":   {"cmd": "vcs -full64 -sverilog -timescale=1ns/1ps -top {top} -f {filelist} -Mdir={workdir}/csrc -o {workdir}/simv", "parse": "vcs", "supports": ["sva"]},
    "run":   {"cmd": "{workdir}/simv +TC={tc}", "parse": "vcs"}
  }
}
```

Placeholders: `{top} {filelist} {files} {incdirs} {workdir} {project}` and whatever the step passes (a list expands to
several arguments). `parse`: `vcs | dc | verilator | yosys | iverilog | vivado | verible | generic` or `{"error": [regex], "warning": [regex]}`.
`supports: ["sva"]` declares a capability; a step that needs one the role lacks reports the check as **N/A**, never as
passed. `q3tui.eda.tools.run_role` runs a role (modules / setup script wrapped like the vendor adapters), keeps the log
and returns normalised diagnostics; `describe_for_llm` tells a stage which roles exist. Stages that may run a tool
themselves get only the allow-listed command through `Bash`; anything computable (lint, simulation, mutation) is
run by code and handed over as ground truth.

## The picker (TUI)

Like Claude Code's question picker: a list of numbered options (↑/↓, Enter, number keys; Space toggles in
multi-select), each with a description, an always-present **Other…** (opens the multi-line text prompt) and an optional
preview panel. Used for: answering a question (its suggested answers, the default marked *(recommended)*), the gate
dialog (options follow the gate's state: Approve is hidden while blocking questions are open), reset scope,
proposals, confirms, and the assistant's `ask_user` tool. A question carries `options: [{label, description?, impact?}]` (`impact`: the option's effect on area, speed, power, complexity)
(optional; the LLM proposes 2–4 when the answer is a choice); without them the picker shows the default assumption and
Other. The answer still goes through `Ops.answer`.

## Panels

The pipeline list and each step's panel come from the flow. A step kind registers a panel builder
(`tui/panels.py: register_panel(kind, builder)`) returning the tabs to show; a kind without one gets the default panel
(its output files, Questions, Log). `spec` has the Sections panel; the spec flow's kinds add tables over their JSON artifacts
(`tui/vlsit_panels.py`, docs/spec/tui.md).

## Questions always come with options

Every question has a pick list in the TUI (and `q3tui answer`, the assistant): the options its step proposed, else
`steps/common.py: derive_options` — `yes` / `no` for a yes-no question ("Does…", "Is…", "Should…"), otherwise the assumed answer
followed by `Decide for me` / `Not needed`; "Other…" always lets you type. `Question.options` tells LLM stages to always propose
2–4 answers. Applied in `Engine.questions()`, so every consumer sees them.
