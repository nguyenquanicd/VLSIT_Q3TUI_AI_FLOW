# Q3TUI Architecture

## Layers

```text
 ┌───────────────────────────────────────────────────────────────────────────┐
 │ CLI (click): run · status · approve · answer · gate · sign …  │  TUI       │
 └──────────────┬──────────────────────────────────────────────────┬─────────┘
                │            event bus (typed events) ◄─────────────┘
 ┌──────────────▼────────────────────────────────────────────────────────────┐
 │ Pipeline engine: flow (step graph), staleness (hashes), gates, fix loop,  │
 │ pipeline.json                                                             │
 ├───────────────────────────────────────────────────────────────────────────┤
 │ Step kinds: spec · vlsit_parse · vlsit_config · vlsit_rtl · vlsit_tb ·    │
 │   vlsit_sva · vlsit_verify · vlsit_doc   (each = ordered stages:          │
 │   deterministic / LLM / tool)                                             │
 └──────┬──────────────────────┬──────────────────────────┬──────────────────┘
        │ LLM stages           │ deterministic            │ tool runs
 ┌──────▼─────────┐   ┌────────▼──────────┐   ┌───────────▼────────────────────┐
 │ LLM runtime    │   │ HDL layer         │   │ Tools by role (tools.json)     │
 │ Claude Agent   │   │ filelists, pyslang│   │ lint · synth · sim · run ·     │
 │ SDK: query(),  │   │ parse, rule checks│   │ mutate · pdf                   │
 │ in-proc tools, │   │ loops, log parse  │   │ via env modules / setup script │
 │ structured out │   │                   │   │ (vendor adapters: q3tui.eda)   │
 └────────────────┘   └───────────────────┘   └─────────────────────────────────┘
```

## LLM stage contract

Implemented in `q3tui.llm.runtime`.

| Aspect | Rule |
|---|---|
| Harness | `claude_agent_sdk.query()` per stage. Sessions: `fresh` (a session per stage; stages communicate only through files) or `flow` (one persistent session for the whole flow), see [flows.md](flows.md) "Sessions"; team mode routes tasks to role sessions ([team.md](team.md)) |
| Model | `llm.model` (default `claude-sonnet-5-5`), `llm.effort`; per step `llm.steps` |
| Working dir | project root |
| Tools | explicit allow-list per stage. Read-only stages: `Read`, `Grep`, `Glob`. Code-writing stages add `Write`/`Edit` |
| Visibility | enforced with a `PreToolUse` hook: `deny_dirs` (e.g. the TB stages are denied `src/rtl`), `write_dirs` / `write_files`; `.q3tui/` is always denied. The hook is the guarantee; the prompt instruction is backup |
| Q3TUI tools | in-process MCP server `q3tui` (`mcp__q3tui__*`): design facts, safe glob, step tools. Tools the LLM calls to close its own loop |
| Permissions | `permission_mode="dontAsk"`; nothing outside the allow-list runs; `Bash` only for an allow-listed tool-role command |
| Output | pydantic model → JSON schema → `output_format`; validated on return, re-asked in the same session |
| Budget | optional `max_budget_usd` per stage; `llm.stage_token_budget` flags runaway stages; cost recorded per run |
| Logging | every SDK message → `.q3tui/runs/<id>/events.jsonl` |

## Tools

Steps call a **role** (`q3tui.eda.tools.run_role(tools, "lint", …)`), never a binary. The
tools file maps each role to a command template and a log parser; diagnostics are
normalised to `{severity, code, file, line, message}` and raw logs are kept in the step's
work dir. A capability a role lacks (`supports: [sva]`) makes the check **N/A**, never
passed. Details: [flows.md](flows.md) "Tools by role".

The older vendor adapters (`q3tui.eda`: `slang`, VCS) remain underneath: `q3tui design
check` uses them, and `tools.syntax` / `simulator` / `lint` / `debug` in `q3tui.yaml`
bind them.
