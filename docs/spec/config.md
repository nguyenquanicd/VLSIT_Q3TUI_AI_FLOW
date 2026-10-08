# Q3TUI Configuration — `q3tui.yaml`

## Resolution order (layers merge; later wins)

1. Built-in defaults
2. `$Q3TUI_HOME/q3tui.yaml` (default `~/.q3tui`) — site settings: tool modules, default model
3. `<project>/q3tui.yaml` — per-design overrides
4. `q3tui --config <path>` or `$Q3TUI_CONFIG`
5. Command-line flags: `q3tui --model <id> [--effort <level>] ...`

In the TUI, `,` (or `/settings [tab]`) opens the **settings dialog**: tabs General, Gates, LLM,
Spec, RTL, TB and Models per step, built from `q3tui/core/settings.py`.
Changes are validated against this schema, apply from the next stage on, and are written
to `<project>/q3tui.yaml` unless "Only for this session" is ticked. The assistant
has the same (`get_settings` / `set_settings`).

Layers are deep-merged, so a project file that only sets `llm.model` keeps the user
file's tool modules. Unknown keys are rejected, except the settings of the removed `units` /
`steps` flows (`req`, `arch`, `model`, `sim`, `triage`, `units`, …), which older config files
still carry: they are dropped on load (`config._legacy`). Strings expand `~` and `$ENV`.
`q3tui config show` prints the merged config and which layers it came from.

## Schema

```yaml
llm:
  model: claude-sonnet-5-5          # any model id accepted by the Claude Agent SDK
  fallback_model: null
  effort: high                    # low | medium | high | xhigh | max | null (Haiku 4.5). Like Claude Code: thinking
                                  # is never cut short and the effort never lowered (runaway_thinking_tokens /
                                  # runaway_seconds are ignored); no turn or time limit either
  thinking_budget: 6000           # only when effort is null: cap extended thinking (0 = off, null = model default)
  max_turns: (ignored)            # like Claude Code: no turn limit, Claude decides when a task is done
  max_budget_usd: null            # per LLM stage
  tool_output_limit: 6000         # characters one tool call may return (head + tail kept): results are re-sent every turn
  stage_token_budget: 1000000     # warn when one stage reads more input tokens (cache included); null = never
  steps:                          # per-step model / effort (Settings → "Models per step"); unset = the values above
    rtl:    {model: claude-sonnet-5-5, effort: high}
    tb:     {model: claude-haiku-4-5}               # effort: default | none | low … max ("none" for Haiku)
    verify: {effort: medium}
    # keys: spec parse config rtl tb sva verify doc assistant
  timeout_s: null                 # per LLM stage: null = no limit (like Claude Code); a number only if you want one
  env: {}                         # passed to the SDK, e.g. ANTHROPIC_BASE_URL

project:                          # folder names inside the project
  spec_dir: spec
  state_dir: .q3tui
  req_dir: req                    # req … tb: team-mode role folders (NOTES.md) and independence deny lists
  arch_dir: arch
  model_dir: model
  rtl_dir: rtl
  tb_dir: tb
  sim_dir: sim                    # verify copies its logs to sim/vlsit_verify/ (stages never read .q3tui/)
  schemas_dir: schemas            # the VLSIT flow (flows/vlsit/): JSON artifacts (structured_spec.json, final_config.json, rtm.json, …)
  src_dir: src                    #   src/rtl, src/sva, src/tb
  docs_dir: docs                  #   docs/specification.md (.pdf)

pipeline:
  flow: vlsit                     # the flow: a name (built-in `vlsit` / `example`, or a file / folder in <project>/flows/ or
                                  # ~/.q3tui/flows/) or a path to a flow file (docs/spec/flows.md; `--flow`, `q3tui flow list|check`)
  gates: []                       # steps with a review gate (step ids of the flow). Empty: the flow file's own `gate:`s;
                                  # set: replaces them. `--yes` approves all (even blocking questions)
  session: null                   # null = the flow's (`session:` in the flow file: vlsit = flow); flow = one persistent session for the whole flow; fresh = a session per stage
  gate_modes: {}                  # per-step gate mode: human | auto | auto_answer | none — overrides the flow file's (TUI: g, /gate,
                                  # Settings → Gates; CLI: `q3tui gate STEP MODE`; assistant: set_gate_mode)
  auto_approve: false             # gates approve themselves, but stop for blocking questions (/autoapprove, --auto-approve)
  auto_answer: false              # unattended: gates approve, every question takes its default, proposals accepted (/autoanswer, --auto-answer)
  auto_confirm_reviews: false     # confirm a step's own reviewable items (SVA assertions, flagged REQs) without asking (/autoconfirm)
  max_fix_iterations: 5           # fix rounds (verify: triage → fixes → verify) when the step sets no `max_fix_rounds`
  escalate_after_repeats: 2       # same finding, no progress → ask human
  step_options: {}                # per-step options on top of the flow file's: {sva: {auto_confirm: false}, verify: {max_fix_rounds: 12}}
  step_flow: {}                   # per-step view / pass / notes overrides (/step; docs/spec/flows.md)
  parallel_rtl_tb: false          # vlsit: run rtl and tb together (/parallel)
  parallel_multi_agent: false     # with parallel_rtl_tb: tb gets its own session (role verifier)

spec:
  self_review: true               # review pass after a full draft; returns only its changes (/specreview)

tui:
  icons: unicode                  # unicode (✔ ⚑ ◐ …) | nerd (a "… Nerd Font Mono" font) | ascii (any font)
  language: en                    # en | vi | ko

rtl:                              # vlsit rtl (and sva: modules at once)
  max_fix_attempts: 3             # extra rounds when Q3TUI's own checks (rules, lint, synthesis) still fail
  parallel: 4                     # modules written at once
  max_turns: (ignored)            # like Claude Code: no turn limit, Claude decides when a task is done

tb:                               # vlsit tb
  max_fix_attempts: 3             # extra rounds when the plan / TB checks still fail
  max_turns: (ignored)
  tests_per_agent: 6              # test cases per agent (agents run in parallel)
  parallel: 4                     # test-case agents at once

team:                             # role sessions (team.md)
  enabled: false                  # every role's tasks go to that role's one persistent session (`q3tui team`)
  dedupe_chars: 300               # sections (then paragraphs) at least this long are sent once per session (0: never)
  unit_sessions: own              # own: a module's RTL / a TB group gets its own small session (resumed for its fixes); role: the role's one session
  fork: true                      # parallel tasks of a role fork its session at its last idle point (else they queue)
  max_context: null               # null: Claude compacts a role session itself when it fills up (like Claude Code);
                                  # a token count compacts it after the task that passed it

tools:
  file: null                      # tools file (tools.json / tools.yaml); default: <project>/tools.json|yaml, then ~/.q3tui/
  roles: {}                       # tools by role for the flows (overrides the file's): {lint: {cmd: "vcs -full64 -sverilog +lint=all -f {filelist} …",
                                  #   parse: vcs, supports: [], description: …}, synth: {…}, sim: {…}, run: {…}}; docs/spec/flows.md "Tools"
  timeout_s: 600                  # per tool invocation
  setup_script: null              # sourced before tools
  modules: []                     # `module load`ed before every tool
  modules_init: null              # default $MODULESHOME/init/bash
  syntax:    {adapter: slang}
  simulator: {adapter: vcs, bin: vcs, extra_args: [], modules: []}
  lint:      {adapter: spyglass, bin: sg_shell, rules_file: null, modules: []}
  debug:     {adapter: verdi, bin: verdi, modules: []}

mcp_servers: []                   # extra MCP servers available to LLM stages
```

Tools run as `bash -c 'source <setup_script> && source <modules_init> && module load <tools.modules + role modules> && exec <cmd>'`.
A config for this site is in [`q3tui.example.yaml`](../../q3tui.example.yaml).
