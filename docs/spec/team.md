# Team mode — persistent role sessions

With `team.enabled` (and per-stage sessions, `session: fresh`), every LLM task of a role
goes to **that role's one Claude session**, which persists across tasks, runs and restarts
until you restart it. A persistent session reads the project once (then from the prompt
cache at ~1/10 of the price), remembers why it wrote what it wrote, and fixes a bug report
in a turn. With `session: flow` (the VLSIT default) the whole flow is already one session
(role `flow`, [flows.md](flows.md) "Sessions") and team routing does not apply.

## The roles

`llm/roles.py` routes a task by its stage name (`role_of`):

| Role | Tasks (stage names) | Never sees |
|---|---|---|
| **architect** | `spec_*` (the spec writer and its updates) | model, rtl, tb folders |
| **designer** | `rtl_*` (RTL modules and their fixes) | model, tb |
| **reviewer** | `rtl_review_*`; it never writes RTL | model, tb |
| **verifier** | `tb_*`, `vplan*` (test plans, testbenches) | rtl, model |
| **modeler** | `model_*` (no built-in step has such a stage now) | rtl, tb |
| **flow** | every task, in `session: flow` mode | — (each task keeps its own confinement) |

Not in a role session (independent by design, fresh every time, `_FRESH`): reviewers
(`*review*` other than the RTL reviewer), delegated sub-agents (`*_section_*`, `*_group_*`)
and every stage no role claims (e.g. `parse_spec`, `config_overrides`, `sva_*`,
`verify_*`).

Deterministic work stays code, offered as tools and run again as the gate after every task.

## How a role session works

| Mechanism | What it does |
|---|---|
| **Init once** | a role's first task is preceded by one init turn: the role charter (system prompt, fixed for the role) and a project brief (what the project is, where things are, what the role may read and write, its notes file). Every later task is a new message in the same session |
| **Tasks are messages** | a task's instructions (what would be its stage system prompt) and its brief are sent as the message. Instructions already sent in this session are replaced by a reference; so is every large block the session has already seen unchanged |
| **Stable prefix** | the system prompt is the role charter; the tool list is the union of the role's tools, in a fixed order, starting with `notes` and `submit` (a tool outside the current task answers "not available in this task"). Every task returns its result through the one `submit` tool — the task's schema is in its message and code validates it at once — instead of a per-task structured-output schema, which would change the tool list. Only roles whose tasks write files (modeler, designer, flow) are offered Write / Edit; reading (Read, Grep, Glob) is always allowed inside the role's area. The prompt cache keeps working across tasks |
| **One task at a time; forks** | a role's tasks queue for its session. With `team.fork: true` (default) a task that starts while the session is busy forks it at its last idle point (`resume_session_at` = the last message of its last finished task), so it knows everything the role knew then and never an unfinished task; without a known idle point (after a compaction, before the first task) tasks queue |
| **Safety net** | a task that fails in the role's session (e.g. its result never validates) is done once more in a fresh session with its original prompt; the role's session is told so with its next task |
| **Unit-level work in its own sessions** | `team.unit_sessions: own` (default): a module's RTL or a group of tests (`roles.UNIT_LEVEL`) runs in its own small session, resumed for that unit's fixes; the role session keeps the rest. `role` = everything in the role's one session (it grows large) |
| **Dedupe by section; a role's own products count** | a prompt is split into markdown sections (and, inside a changed one, paragraphs); an unchanged one the session already has (`team.dedupe_chars`, default 300) becomes a one-line reference. What a role's own task wrote is in its session too (`ROLE_PRODUCTS`: the architect's spec) |
| **Notes** | each role keeps `NOTES.md` in its folder (`arch/`, `model/`, `rtl/`, `tb/`; reviewer `rtl/REVIEW_NOTES.md`; flow `schemas/NOTES.md`), written with the `notes` tool: decisions, conventions, open issues — one `section` per block / topic, set on its own (the rest kept); no section = rewrite the whole file. Its text is part of the brief after every compaction |
| **Compaction** | like Claude Code: a role session compacts itself when its context fills up (the SDK's auto-compaction); Q3TUI sees the boundary and re-sends the brief and the notes with the next task (and forgets which blocks it had sent). Opt-in `team.max_context`: after a task whose session passed it, Claude compacts that session (`/compact`, keeping decisions, unit status and open issues, dropping tool output). If compacting fails, the next task starts a fresh session |
| **Persistence** | session ids live in `.q3tui/team/roles.json`. A new `q3tui run` resumes them; a step reset tells the role what was reset, it does not restart it (a full reset in `flow` mode does). `q3tui team reset [role]` starts a role (or all) fresh |

Independence is enforced exactly as without team mode, per task: `deny_dirs`, `write_dirs`
and the `PreToolUse` hook. A role's sessions only ever held its own side's files, so its
memory cannot leak the other side.

## Enabling it

`q3tui.yaml`:

```yaml
pipeline:
  session: fresh       # team routing applies to per-stage sessions (the vlsit flow defaults to `flow`)
team:
  enabled: true        # route every role task to its persistent session
  dedupe_chars: 300    # markdown sections (then paragraphs) at least this long are sent once per session
  fork: true           # parallel tasks of a role fork its session at its last idle point (else they queue)
```

`q3tui team` shows the roles (session, tasks, forks, compactions, cost);
`q3tui team reset [role]` restarts them.

## Costs are per query

A resumed (or forked) session reports the session's running totals, not the query's. The
runtime records every result as the difference to what that session reported before
(`runtime._TOTALS`; role sessions keep theirs in `roles.json`), so costs are per query also
across restarts.
