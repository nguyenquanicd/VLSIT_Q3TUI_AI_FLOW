# TUI

`q3tui` (with no arguments) or `q3tui tui`, run inside a project directory, opens
the terminal UI. It is built with [Textual](https://textual.textualize.io/). It is a
front end over the **same pipeline engine** the CLI uses: anything you can do in the TUI
you can also do with `q3tui run/status/approve/answer`, and the other way round.

Unlike a mode-per-agent chat, the TUI is organised around the pipeline. You see where the
design is, what is running, and what needs your decision.

## Layout

```text
┌─ Q3TUI · fifo_top · flow vlsit · ~/proj/fifo ─────────────── claude-opus-5-5 · $1.84 ─┐
│ PIPELINE            │ STEP 3a · rtl   [Modules] [Synthesis] [Files] [Questions] [Log]   │
│                     │ ✔ NOT REVIEWED …                                                  │
│ ✔ 0  spec           │ …                                                                 │
│ ✔ 1  parse          │                                                                   │
│ ✔ 2  config  auto   ├───────────────────────────────────────────────────────────────────┤
│ ◐ 3a rtl            │ ACTIVITY — 3a rtl                                                 │
│ · 4  tb      auto   │ 14:02:11 rtl    writing src/rtl/fifo_ctrl.sv                      │
│ · 3b sva  0/12 SVA  │ 14:02:40 rtl    → lint  2 warnings                                │
│ · 5  verify         │ 14:02:41        fifo_ctrl.sv:33  width mismatch on count          │
│ · 6  doc            │ 14:03:02 rtl    → lint  clean · → synth …                         │
│                     │                                                                   │
├─────────────────────┴───────────────────────────────────────────────────────────────────┤
│ > make the ingress depth a parameter, default 16                                        │
├─────────────────────────────────────────────────────────────────────────────────────────┤
│ r run · x stop · a review · A approve all · e edit · o questions · i chat · ? help     │
└─────────────────────────────────────────────────────────────────────────────────────────┘
```

| Area | Content |
|---|---|
| Header | design, the flow (`flow vlsit`), project path, model, total LLM cost for the session |
| Pipeline | the steps **of the project's flow** (`engine.pipeline_steps`, tags from `engine.label`; the VLSIT flow: 0 spec, 1 parse, 2 config, 3a rtl, 4 tb, 3b sva, 5 verify, 6 doc): status, stale marker, open gate / question count, **gate mode badge** (`auto`, `auto-ans`; nothing = human), fix-loop iteration. Keys `1`–`9` select by position. Selecting a step filters Activity and Files to it |
| Activity | live event stream of the running step(s): agent messages (collapsed to one line, expandable), tool calls, lint / synthesis / sim results. Steps may run in parallel (`pipeline.parallel_rtl_tb`: rtl ‖ tb), so lines are tagged |
| Result banner | top of each step view: the state of the result shown below it: `⚑ NOT REVIEWED` (a to approve) — or, with a specific reason for the gate still being open, `⚑ QUESTION WAITING` (blocking question(s): open the Questions tab, `o`) / `⚑ CONFIRM NEEDED` (StepDef.pending_reviews() items not confirmed: `c` / `C`) — `✔ APPROVED <time>`, `! OUT OF DATE` (old result shown), `✎ EDITED` by you, `◐ RUNNING` (previous result shown), `✖ LAST RUN FAILED` (last good result shown), `★ YOURS`, `○ NO RESULT YET`. Results are always shown when they exist |
| Section status (spec) | per section: `✔ reviewed`, `⚑ not reviewed`, `✎ edited`, `? TBD`, `↻ change pending`, `○ missing`, `+ extra`. `v` marks a section reviewed; approving the spec marks all; regenerated sections whose text is unchanged keep their review |
| Step view (lower panel) | follows the selected step (and the running one); see "Panels" below for how a flow's steps get theirs. **spec**: Sections (template ↔ spec.md; Enter edits the selected section's text in place, `n` add, `d` delete, `t` required/optional, `[` `]` move, `E` edit template) · Questions · Files. Other kinds: their tables, Files, Questions, Log |
| Files | the selected step's outputs rendered read-only (Markdown rendered, SV highlighted); `e` opens the file in `$EDITOR`, and saving marks downstream steps stale |
| Questions | per step, tagged with where the answer goes (`→ spec`, `→ parse`, `→ config`; untagged = stays in this step): open questions first (`⚑ blocking`, `○ open`), then closed ones (`✔ answered`, `✔ default` accepted on approval; `·old` = no longer asked by the latest output). Enter answers or changes an answer in the **picker** (see below: suggested options, the assumed answer, Other… to type) with the whole question, what was assumed and where the answer goes; the typed answer box is multi-line (Enter = new line, ctrl+s = submit; the same box is used for change requests and section guidance); `h` hides/shows closed |
| Command line | chat with the Q3TUI assistant (below). Lines starting with `/` are commands |

## Panels: any flow, no TUI code

The lower panel of a step comes from its **kind** (`StepDef.kind`, the `kind:` of the flow file's step), through
`tui/panels.py`: `register_panel(kind, builder)` with `builder(step) -> StepPanel`.

- `spec` has the Sections panel above (`SpecPanel`).
- A kind without a registered panel gets the **default panel**: the result banner, tabs **Files** (the step's outputs and
  inputs, rendered), **Questions**, **Log** (what the step did: its events, the last 300 lines).
- The VLSIT kinds (`tui/vlsit_panels.py`) are the default panel plus tables over the JSON artifacts (each row has a detail
  pane; missing files or keys only mean an empty table):

| Kind | Tabs (before Files / Questions) |
|---|---|
| `vlsit_parse` | **Requirements** (REQ, category, ambiguity score, state: `review` when > 0.3, `decide` when it needs your decision; detail: text, source, SVA hint, parameters, RTL modules) · **Parameters** |
| `vlsit_config` | **Parameters** (value, default, changed, locked, confirmed) · **Constraints** (C1–C5 ✔/✖ with rule and detail) |
| `vlsit_rtl` | **Modules** (file, lines, `// REQ-xxx` tags, cells, area; source in the detail) · **Synthesis** (`synth_report.json`) |
| `vlsit_tb` | **Test plan** (TC ↔ REQs, selected) · **Uncovered REQs** (no selected test case covers them) |
| `vlsit_sva` | **Assertions** (label, module, REQs, vacuity, review status; the `// NL:` text in the detail). `c` confirm · `x` reject (asks why; the SVA step rewrites it) · `u` back to pending · `C` confirm all. Only confirmed assertions count in the RTM |
| `vlsit_verify` | **RTM** (the six conditions per REQ, mutation score, sign-off; detail: each condition with its evidence). `v` signs the highlighted requirement off (a note is asked; refused unless RTL, SVA, TC, simulation and mutation hold), `V` takes it back · **Simulation** (per-TC results) |
| `vlsit_doc` | Files · Questions |

The pipeline list also shows `n/m SVA` (assertions confirmed) and `n/m REQ` (signed off). The keys call
`Ops.review_property`, `Ops.confirm_properties`, `Ops.sign_req` (wrappers of `flows/vlsit/sva/review.py`); the assistant has
the same as tools (`list_properties`, `review_property`, `confirm_all_properties`, `rtm_status`, `sign_requirement`).

## The picker

Choosing is a **picker**, like Claude Code's question dialog (`tui/picker.py`, `ChoiceScreen`):

```text
┌─ Answer Q-R3 (blocking) ────────────────────────────────────────────┐
│ What is the FIFO depth?                                              │
│ Assumed so far: 16                                                   │
│ Your answer goes to: spec                                            │
│                                                                      │
│ ▶ 1. 16 (recommended)    what is assumed in the text now             │
│   2. 8                   tiny: 2 banks of 4                          │
│   3. 32                  twice the traffic, more area                │
│   4. Other…              type your own answer                        │
│ ↑/↓ or number: choose · Enter: pick · Esc: cancel · Other… lets you type │
└──────────────────────────────────────────────────────────────────────┘
```

- **Keys**: ↑/↓ and Enter, or the option's number; Esc cancels. **Other…** (always last) opens the multi-line text
  prompt (Enter = new line, ctrl+s = submit), pre-filled with your current answer or the assumption. Multi-select: Space
  toggles, Enter picks the highlighted (or the ticked), ctrl+s confirms. A choice may carry a one-letter key (`a`, `q`, …)
  and a **preview** pane (shown while it is highlighted).
- **Questions** (Enter in the Questions tab, `o`, the gate's "Answer questions", `answer_question` by the assistant): the
  options are the question's own `options` (the LLM proposes 2–4 when the answer is a choice: `Question.options`, each
  `{label, description}`); the assumed answer is added first and marked *(recommended)* when it is not among them; your
  current answer is marked. A step that only proposes (`StepDef.proposes_only`) offers **Accept the proposal / Reject / Edit the proposal…**.
  The answer still goes through `Ops.answer` → `engine.answer`, which routes it to the step that owns it.
- **Gates**: the review dialog is a picker too — Approve (hidden while blocking questions are open) · Answer questions ·
  Edit · Request changes · Later, with the gate's mode in the text. Keys `a q e r l` still work.
- **Reset** (`R`: this step / all steps / cancel), **confirmations** (destructive actions, `y` / `n`), the **gate mode** (below).
- **The assistant** can ask you through the same dialog: its `ask_user` tool takes up to 4 questions with options (and
  `multi`), and returns your answers.

## Gate modes

Every review gate has a mode (docs/spec/flows.md "Gate modes"): **human** (stops for you), **auto** (approves itself; blocking
questions still stop it), **auto-answer** (unattended: approves itself and every question takes its default), **none**
(no gate). The flow file sets the defaults; `pipeline.gate_modes` overrides them.

- The pipeline list marks `auto` / `auto-ans` gates; the review dialog says which mode it is in.
- `g` opens a picker for the selected step's mode; `/gate <step> [mode] [--session]` (no mode: the picker; saved to
  `q3tui.yaml` unless `--session`); `/gates` lists them all; Settings → **Gates** has one selector per step.
  The assistant: `gates`, `set_gate_mode` (turning a gate to auto / auto-answer asks you). CLI: `q3tui gate STEP MODE`.
  All of them call `Ops.set_gate_mode`.
- `/autoapprove` and `/autoanswer` (the header's AUTO-APPROVE / AUTO-ANSWER badge) stay as project-wide switches on top.

## Approving continues the run

Approving in the TUI — `a` in the review popup, `A` (approve all), `/approve <step>|all` —
carries on with the pipeline by itself: decisions you made (answers, accepted defaults)
flow into the steps they belong to (spec, parse, config, …) as targeted
updates, and the run stops again at the next review or blocking question. With
auto-approve on, it runs through reviews too. (`q3tui approve` on the CLI only
approves; run `q3tui run` to continue.)

## Reset

`R` asks what to reset: **(s)** the selected step and the steps after it, or **(a)** all
steps (the whole design from scratch). Each choice lists the files it deletes. Your inputs
(intent, template, your own files) are kept, and deleted files are backed up to
`.q3tui/bkp/` (never visible to agents). `/reset <step> [--only]` and `/reset all` do
the same from the command line; the assistant's conversation restarts after a reset.

## Resizing

Every pane boundary is a draggable bar: the pipeline column | the rest, step view ─
activity (step view on top, activity below it), and the columns inside step views (spec
sections | text, tables | detail). Drag to resize, double-click a bar to go back to the default. Keyboard:
`ctrl+←/→` pipeline width, `ctrl+↑/↓` step view height (activity takes the rest). Sizes
are remembered per project (`.q3tui/tui/layout.json`); `z` still zooms the activity
to full height.

## Settings

`,` or `/settings [general|gates|llm|models|spec|rtl|tb]` opens the settings dialog.
Tabs: **General** (auto-approve, auto-answer, auto-confirm, rtl ‖ tb in parallel, fix loop, language, icon set: `unicode` ✔ ⚑ ◐, `nerd` for a
"… Nerd Font Mono" font, `ascii` for any font), **Gates** (the mode of each step's review gate in the current flow), **LLM** (model, effort, thinking budget,
turns, $ and time limits, fallback model), **Spec**, **RTL**, **TB** and **Models per step**
(model / effort per step); it opens on the selected step's tab. Each
setting has a one-line help. `ctrl+s` applies and saves to the project's `q3tui.yaml`; tick "Only for this
session" to try a setting without saving it; invalid values keep the dialog open with the error.

## Statistics

`s` or `/stats` (CLI: `q3tui stats [--since DATE]`, assistant: `show_stats`) shows
usage: **Overview** (cost, LLM stages and turns, input tokens split into fresh / cache
read / cache write with the cache-hit rate, output tokens incl. thinking, LLM time, step
wall time), **By model**, **By step** (incl. the assistant), **By stage** (sorted by
time spent) and **Recent stages**. `t` switches between all time and this TUI session;
`R` (confirmed), `/stats reset`, `q3tui stats --reset` or the assistant's `reset_stats`
start the statistics over (the run logs are kept).
Data: `.q3tui/stats.jsonl`, one line per finished LLM stage (from the Agent SDK's
per-model usage) and per step; built once from the run logs for older projects — only when
the file is missing, so a reset empties it (deleting it brings every old run back).

## Gates

When the pipeline reaches a gate, it pauses and a modal opens:

```text
┌─ Review: 1 parse ───────────────────────────────────────────────┐
│ 14 requirements · 6 parameters · 3 modules                      │
│ 2 open questions (1 blocking)                                   │
│ Gate mode: human (you review it) · /gate parse <mode> changes it │
│                                                                 │
│ ▶ 1. Answer questions (q)   2 open, 1 blocking                  │
│   2. Edit (e)                                                   │
│   3. Request changes (r)                                        │
│   4. Later (l)                                                  │
└─────────────────────────────────────────────────────────────────┘
```

The options follow the state: **Approve (a)** is offered only when no blocking question is open.

"Request changes" re-runs the step with your comment added to its prompt. The gate
state lives in `pipeline.json`, so you can quit and approve later from the TUI or with
`q3tui approve parse`.

## Assistant (command line)

Free text goes to the Q3TUI assistant. **Everything the user can do in the TUI or
CLI, the assistant can do by prompting.** Both go through the same operations layer
(`q3tui/core/ops.py`), so a key press and a prompt behave identically.

| Area | Tools |
|---|---|
| Status | `pipeline_status`, `list_questions`, `show_cost` |
| Spec template | `get_template`, `add_section`, `update_section` (title, guidance, required, fields), `remove_section`, `move_section` |
| Spec content | `write_section` (write or replace one section of `spec.md` now; creates it at its template position), `set_intent`, `append_intent`, `mark_reviewed` |
| Gates and asking | `gates`, `set_gate_mode`, `ask_user` (the picker: up to 4 questions with options, answers come back as text) |
| Flow | `set_step_setting` (view / pass / notes), `get_skill`, `set_skill`, `get_prompt`, `set_prompt` |
| VLSIT flow | `review_parse_requirement`, `confirm_all_requirements`, `review_config_parameter`, `confirm_all_parameters`, `list_properties`, `review_property`, `confirm_all_properties`, `rtm_status`, `sign_requirement`, `sign_all_ready_requirements` |
| Pipeline | `run_steps` (start/stop/only/regenerate), `stop_run`, `approve`, `answer_question`, `request_change` (optionally for one section), `reset_step`, `import_file` (a spec) |
| Settings | `set_model`, `set_spec_review`, `set_auto_approve`, `set_auto_answer`, `set_auto_confirm_reviews`, `get_settings`, `set_settings`, `show_stats`, `reset_stats`, `reset_cost` |
| Files | `Read`/`Grep`/`Glob` over the project; `Edit`/`Write` only in `spec/`, `schemas/` and `src/` (like a user in an editor); never `.q3tui/` |

Example: *"add a Power Management section after Performance with a field Power states,
and write it"* → `get_template`, `add_section`, `write_section`.

**Steps stay separate.** The assistant works on the step the user is viewing when they
send the message (the `[TUI context]` line):

- **Spec problems are handed off.** If it finds a problem in the spec while working on a
  later step, it queues `request_change(step='spec')` and reruns only the spec
  (`run_steps(only='spec', wait=true)`). This is allowed without asking.
- **Anything else on another step asks first.** Approving, resetting, editing, answering
  its questions or running it opens a confirmation dialog, so the user decides and not
  the model.
- **No promises.** It cannot act after its reply. With `run_steps(wait=true)` it can
  finish a run and continue (e.g. approve) in the same turn.

Destructive actions (`reset_step`, `approve` with force, `reset_cost`) open a
confirmation dialog in the TUI, so the user decides, not the model. The assistant only
approves when asked. Its conversation is resumed across TUI launches (Agent-SDK session
id in `.q3tui/tui/session.json`; `/newchat` starts over).

Slash commands complete as you type: the rest of the command or argument shows as grey ghost text (Tab or → accepts it) and a hint line above the input shows the usage and the valid values (steps, open question ids, models, efforts, files). Slash commands, which need no LLM: `/run [from] [to]`, `/stop`, `/approve <gate>|all [--force]`,
`/change <step> <text>`, `/answer <id> <text>|all`, `/gate <step> [mode] [--session]`, `/gates`, `/flow`, `/skill`, `/prompts`,
`/step <step> view|pass|notes [text]`, `/reset <step>|all [--only]`, `/model [id] [--save]` (no id: the settings' LLM tab),
`/effort <level>`, `/specreview`, `/autoapprove`, `/autoanswer`, `/autoconfirm` (`on|off [--save]`), `/parallel on|off
[--multi-agent]`, `/settings [tab]`, `/status`, `/open <file>`, `/cost [reset]`, `/stats [reset]`, `/newchat`, `/help`, `/quit`.

Assistant replies render as Markdown; `z` zooms the activity/chat to full height. A new message while the assistant is still answering is held (not sent) instead of cancelling the reply. **Esc** (or `x` / `/stop` when no pipeline step is running) stops the reply; the conversation continues from the last finished reply.

Focus starts on the pipeline so single-key actions work; `i` (or `/`) moves to the
command line and `Esc` returns.

## Architecture

```text
 Textual app ──(calls)──► Pipeline engine (asyncio task in the same process)
      ▲                         │
      └──── event bus ◄─────────┘  same events also appended to .q3tui/runs/*/events.jsonl
```

- The engine emits typed events (`step_started`, `stage`, `llm_message`, `tool_call`,
  `tool_result`, `gate_opened`, `question`, `step_finished`, `cost`) to an in-process
  bus. The CLI prints them, the TUI renders them, and the log file stores them.
- Long runs keep going if the TUI is closed only when started via `q3tui run --detach`
  (later). In v1 the pipeline stops when the TUI exits and resumes from `pipeline.json`
  next time.

**Comment (`C`, every view):** select text with the mouse, or highlight a row / section in the shown tab (requirement, port, test,
assertion, question, spec section …), press `C`, type the comment: the assistant receives it with the quoted text and fixes it with
its tools (change request for the step, or a section edit). One key everywhere; `c` stays free for the views' own actions
(e.g. Confirm in the SVA properties).
