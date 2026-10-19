# Q3TUI

Our own multi-agent chip design/verification platform.

- Our design + roadmap: `docs/spec/` — keep it in sync when behaviour changes. Current milestone is in `docs/spec/README.md`.

## Stack

- Python ≥ 3.11, `uv` (`uv sync`, `uv run pytest`), package `q3tui/` at the repo root.
- LLM: **Claude Agent SDK** (`claude-agent-sdk`) only — all LLM calls go through `q3tui.llm.runtime.run_stage`. Default model `claude-opus-5-5` (config `llm.model`).
- RTL parsing: `pyslang` (`q3tui.hdl.parse`). EDA tools: pluggable, vendor-agnostic role adapters in `q3tui.eda` (lint, synthesis, simulation, formal, debug); agents never call vendor binaries directly.

## Layout

- `core/` — CLI, config, settings, project layout, events, stats, language, icons, ops; `llm/` — runtime, roles, prompts, TUI assistant; `eda/` — tool adapters, tools-by-role (`eda/tools.py`, `eda/presets/`); `hdl/` — filelist + pyslang parsing.
- `pipeline/engine.py` — step graph, content-hash staleness, gates, spec imports; `pipeline.json` state.
- Flow (`pipeline.flow`, default **`vlsit`**); built-in flows: `vlsit` and `example` (a minimal folder flow: spec → parse).
- Flows are data (`flows/__init__.py`, `q3tui/flows/<name>/`, docs/spec/flows.md): `pipeline.flow: <name|path>` picks a flow file (project `flows/`, `~/.q3tui/flows/`, built-in) that lists step **kinds** (`steps.KINDS`, `register_kind`), deps, gate modes (`human|auto|auto_answer|none`; `pipeline.gate_modes` overrides, `Ops.set_gate_mode`), options, `gap_targets`, `sees`. `vlsit` (docs/spec/vlsit-flow.md) is the VLSIT RTL flow, a folder `q3tui/flows/vlsit/` with one folder per step (`step.py`, `md/` (skill.md + the original prompts), `rules/`, `schemas/`, `scripts/`; shared helpers in `steps/vlsit/`); flows can be folders, files, markdown (docs/spec/flows.md). Engine code never hardcodes step names for a flow: use `engine.pipeline_steps`, `engine.label`, `engine.gate_modes()`; TUI panels are registered per step kind (`tui/panels.py`).
- Sessions: `session: flow` in a flow file (vlsit) = one persistent Claude session for the whole flow (role `flow` in `llm/roles.py`, `engine.session_mode()`), `fresh` = per stage; `pipeline.session` overrides.
- Tools by role (`eda/tools.py`, `tools.json` / `tools.roles`): steps call `run_role(tools, "lint", …)`, never a binary; the file gives command templates + log parser; a missing capability (`supports: [sva]`) is reported N/A, never passed.
- Step code: `steps/spec/` (the spec step, kind `spec`), `flows/vlsit/<step>/step.py` (the VLSIT kinds `vlsit_*`), shared pieces in `steps/common.py` (questions, answers, scoped change requests) and `steps/vlsit/`. A step is a `StepDef` subclass that declares inputs/outputs; the engine decides when it runs.
- `core/events.py` — event bus; CLI printer, TUI and `events.jsonl` all subscribe.
- `core/ops.py` — every user action (template/section edits, write_section, review, answer, approve, change, reset, run, model, cost). TUI keys and assistant tools both call it; add new user actions here and expose them in both.
- `tui/` — Textual app over the same engine; `llm/assistant.py` — TUI chat with a tool for every op (destructive ones confirm via the TUI).

## Conventions

- **Changes propagate as updates, not re-runs** (user rule; docs/spec/pipeline.md "Changes propagate…"): every step snapshots what it was built from (`step_meta`), diffs by code on the next run, skips the LLM when nothing relevant changed (set `ctx.unchanged` → approval kept), and otherwise asks for a patch of only the affected parts, applied by code. Full regeneration only on first run, reset or `--regenerate`. Upstream answers/defaults are passed down as settled decisions (`engine.settled_questions`, `drop_settled`). Questions carry a kind saying which earlier step owns the answer (`spec_gap` / `req_gap` / `arch_gap` / `design_choice`; the flow's `gap_targets` map them to its steps — vlsit: `req_gap` → parse, `arch_gap` → config; `engine.gap_target`, `push_decision`): a later step never works around an earlier step's problem, it hands it back. New steps must follow this. Change requests may be scoped to one unit (`[rtl:<module>] …`, `scoped` / `split_feedback`): steps with units re-run only the units named (verify dispatches fixes this way).
- Deterministic first: anything computable (parsing, tool runs, rendering) is code; LLM stages get it as ground truth via prompts or in-process MCP tools (`mcp__q3tui__*`).
- Agents return content as data where they can; code writes the files. Prompts carry only what the unit needs, static parts first; fixes go to small sessions with the problems and the current content. Tool output is capped centrally (`llm.tool_output_limit`); `llm.stage_token_budget` flags runaway stages.
- An LLM stage = `Stage(...)` with an explicit builtin tool allow-list (read-only stages: `Read`, `Grep`, `Glob`), `permission_mode="dontAsk"`, and a pydantic `output_model` for structured output.
- Steps write deliverables to the project folders (vlsit: `spec/ schemas/ src/{rtl,sva,tb}/ docs/`) and internals to `.q3tui/` (see `docs/spec/project-layout.md`); `project.backup()` before overwriting.
- File access: a `PreToolUse` hook confines every LLM stage to `Stage.cwd` + `add_dirs` and blocks `deny_dirs` (use this for RTL/TB independence). `.q3tui/` (backups, run logs) is always denied (added in `StepContext.llm`) and has an `.ignore` so Grep skips it; the built-in Glob ignores `.ignore`, so `runtime.build_options` swaps it for `mcp__q3tui__glob` (`safe_glob`: lists only allowed, non-hidden, non-denied files) — never let old outputs feed a stage.
- Structured output is not strictly enforced by the SDK: `run_stage` validates with pydantic and re-asks in the same session; `output_schema()` must keep field names intact.
- Performance: the TUI refreshes on events (debounced, visible panel only). Keep hot paths free of DOM-wide `query()`, cache file-derived data by mtime (templates, spec sections, model, file hashes), use the single-widget Rich `Markdown` in `tui/views.py` (Textual's Markdown widget mounts thousands of nodes), and import the Agent SDK / pyslang lazily (CLI start ~0.35 s).
- Questions can carry `options` (2–4 suggested answers); the TUI picker (`ChoiceScreen`) shows them + "Other…" — answers still go through `Ops.answer`.
- Tests must not call a live LLM: monkeypatch `q3tui.llm.runtime.run_stage` with `tests/fakes.FakeLLM` (the VLSIT stages: handlers registered in `tests/vlsit_fakes.py`). Engine / TUI tests that are not about a flow run the small test flow `tests/fakes.use_test_flow` (spec, then code-only `rtl` and `tb`). TUI tests use Textual's `run_test` pilot. EDA tools are usually absent — test command construction and log parsing, not tool runs.
- Cheap live checks: a project `q3tui.yaml` with `llm: {model: claude-haiku-4-5, effort: null}`.
- Independence (user decision): RTL never sees the testbench or the SVA; the testbench sees only the spec and the requirements (vlsit `sees:` in FLOW.md for decisions). Enforce with `deny_dirs`.
- Team mode (docs/spec/team.md, `team.enabled`): `llm/roles.py` routes each task by stage name (`role_of`) to its role's persistent session (architect / modeler / designer / verifier). Keep a role's system prompt and tool list stable (no unit names in tool descriptions: the prompt cache); stage instructions go in the message. New reviewers / delegates / investigators must stay fresh (`_FRESH`).
- Examples: `examples/apb_slave` (a VLSIT project).
