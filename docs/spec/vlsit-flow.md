# The VLSIT flow (`flows/vlsit/`)

Ported from `VLSIT_RTL_Generator_AI_Model` (`/autoflow` and its 9 command prompts; the orchestrator in
`flows/vlsit/md/autoflow.md`, each step's stage prompts in its `md/skill.md`, Vietnamese — they keep that wording). Same
phases, same JSON artifacts (original JSON Schemas in each step's `schemas/`, validated by `steps/vlsit/artifacts.py`),
same gates, but run by Q3TUI's engine: staleness, update-not-rerun, questions, reset, events, TUI, assistant. It is
Q3TUI's default flow.

```text
spec ─► parse ─► config ─► rtl (3a) ─► tb (4) ─► sva (3b) ─► verify (5) ─► doc (6)
 human    auto     Gate 1   Gate 2              Gate 4    Gate 3b    Gate 5
                   human    auto                auto      human      human   (defaults; `pipeline.gate_modes`, `q3tui gate`)
```

No architecture step and no golden model: the evidence is SVA, the plain-SystemVerilog testbench and mutation testing,
tied together by the requirements traceability matrix (RTM). Tools come from `tools.json` (docs/spec/flows.md): roles
`lint` (VCS `-lint=all`, or SpyGlass), `synth` (Synopsys Design Compiler, `parse: dc`; Yosys is the alternative backend, `parse: yosys`), `sim` (VCS compile, `supports: [sva]`), `run` (simv) and optionally `mutate`; a role that is
not configured makes its steps wait (`missing_input`) with a message naming the role; a capability a tool lacks
(`supports: ["sva"]`) makes the check **N/A**, never passed.

## Starting point: an intent or a spec

The flow starts at `spec`, which is Q3TUI's spec step with the VLSIT layout (`options: {template: vlsit}`; the original
`/spec_writer` / `/autoflow --describe`):
- **an intent** (`spec/intent.md`, `q3tui run --intent "…"` / `--intent-file FILE`): `spec` writes `spec/spec.md` section by
  section (the 21 sections of the VLSIT ASIC IP Design Specification template: Purpose and Scope … Requirements REQ-<AREA>-<NNN>,
  Features FEAT-NNN, Parameters PARA_* / LPARA_* with constraints C1…, Interfaces, Architecture, Functional Behavior, Register pointer to
  the CSR workbook, RTL contract, Verification, Traceability, Assumptions …), reviews it, and asks what the intent leaves open (questions; the spec gate is human by default).
  What you answer is written into the spec, and `parse` follows the changed sections only.
- **a spec** (`--spec FILE`, or a file in `spec/`): `spec` counts as done (user-provided, never rewritten) and the flow starts at `parse`.
A model that puts a table into an invented field of the template gets it moved into the section body (the Parameters table).
`examples/fifo_sync_vlsit/` is an intent-only project.

## One session

`flows/vlsit/` has `session: flow`: like the original `/autoflow`, the whole flow runs in **one persistent Claude session**
(docs/spec/flows.md "Sessions"): the RTL author remembers its choices when it writes the assertions, the spec is read once.
Consequences: tasks run one at a time (no per-module parallelism); the testbench is not independent of the RTL any more
(the stage still cannot *read* `src/rtl`, but the session has seen it); a session that grows is compacted by Claude.
`pipeline.session: fresh` (or `session: fresh` in a copy of the flow file) gives every stage its own session again.

### rtl ‖ tb in parallel

`rtl` (lint + synthesis) and `tb` (the plain-SystemVerilog testbench generator) both read only `spec`/`parse`/`config`
(neither sees the other's output — see "Starting point" above and `FLOW.md` `sees`); nothing stops them from running
together instead of one after the other. `pipeline.parallel_rtl_tb` (TUI: Settings → General, or `/parallel on|off`)
turns this on; off (the default) keeps the diagram above, `rtl (3a)` then `tb (4)`.

Because of "One session" above, turning it on alone is not full concurrency: the flow's one shared session runs one
task at a time, so `tb`'s LLM calls queue behind `rtl`'s and only their tool runs (lint, synth, sim) truly overlap.
`pipeline.parallel_multi_agent` (only meaningful with `parallel_rtl_tb` on; `/parallel on --multi-agent`) gives `tb`
its own persistent session — role `verifier`, the same charter it would get outside a `session: flow` run — so both
sets of LLM calls run at the same time too, at the cost of the single-conversation consistency "One session" above
describes (the testbench author no longer shares the RTL author's running context).

## Layout (`project.schemas_dir / src_dir / docs_dir`)

```text
spec/<ip>_spec.md                       input (or spec step's output)
schemas/structured_spec.json            parse    Gate 1   (metadata.gate_status, approved_at)
schemas/final_config.json               config   Gate 2   (metadata.gate_2_approved, …)
schemas/synth_report.json               rtl
schemas/selected_testplan.json          tb       Gate 4   (metadata.gate_4_approved, gate_4_at)
schemas/rtm.json                        sva (Gate 3b: gate_3_approved/at), verify (Gate 5: gate_5_approved/at)
schemas/verification_report.json        verify
schemas/sva_reviews.json                your per-assertion reviews (kept on reset)
schemas/questions/<step>.{json,answers.json,md}
src/rtl/*.sv filelist.f                 rtl        src/tb/*.sv filelist.f (+ run script)        src/sva/*.sv bind + GATE3_REVIEW.md
docs/specification.md (.pdf)            doc
.q3tui/…                            internal (sim logs, mutation work dirs, backups)
```

## Steps

| id | kind | LLM stages (read-only unless said) | Code | Tool roles | Gate |
|---|---|---|---|---|---|
| spec | `spec` (option `template: vlsit`) | as Q3TUI's | | | human |
| parse | `vlsit_parse` | `parse_spec`: REQs (`REQ-NNN`, category, `ambiguity_score`, `sva_hint`, feature / parameters affected), parameters, module mapping, ambiguity questions | assemble + validate `structured_spec.json`; `needs_review` when score > 0.3; ambiguous REQs become questions (`spec_gap`; blocking when score ≥ 0.6 after answers cannot settle it); stable ids across updates | | 1 human |
| config | `vlsit_config` | `config_overrides` (only when the spec / feedback asks for non-default values) | parameters locked (defaults unless overridden), constraints C1–C5 evaluated by code, `final_config.json` | | 2 auto (only when all constraints pass) |
| rtl | `vlsit_rtl` | `rtl_<module>` per module (Write only its own file), `rtl_fix_<module>` (fresh small session with the problems and the current file) | module plan from `parse` mapping (LLM `rtl_plan` when absent), rule checker (naming prefixes, reset/clock rules, forbidden constructs via pyslang; rule files = `options.rules`), `filelist.f`, `// REQ-xxx` tag tracing, lint 0 warnings, Yosys synthesis → `synth_report.json` | lint, synth | none |
| tb | `vlsit_tb` | `tb_plan` (TC ↔ REQ), `tb_<tc group>` (Write tb files) — sees spec, parse, config only | REQ coverage warnings (uncovered REQ: warn before Gate 4, never silently), framework, compile check against the RTL, `selected_testplan.json` | sim | 4 auto when compile passes |
| sva | `vlsit_sva` | `sva_<module>` per module: assertions each preceded by `// NL:` | bind + `filelist_sva.f`, compile check, vacuity (cover / smoke) result, `GATE3_REVIEW.md`, RTM rows (`sva_traced` only for assertions you confirmed) | sim (`supports: [sva]`) | 3b human |
| verify | `vlsit_verify` | `verify_triage` (single-shot, no tools): classify failures → rtl / tb / sva, dispatched as scoped change requests; a test case it is unsure about goes to `verify_investigate` (Read / Grep / Glob over the full log, the test, tb_top, RTL and SVA; logs are copied to `sim/vlsit_verify/` because stages never read `.q3tui/`); bounded fix loop (`options.max_fix_rounds`, 8 in `flows/vlsit/`; else `pipeline.max_fix_iterations`) | per-TC simulation, SVA violations, mutation testing (`mutate` role or the built-in mutator + `sim` / `run`), the six RTM conditions, `verification_report.json` | sim, run, (mutate) | 5 human |
| doc | `vlsit_doc` | optional prose stage | `docs/specification.md` from the artifacts + RTL, PDF when a tool is configured (`pdf` role) | (pdf) | none |

### The RTM's six conditions per REQ
(1) RTL traced (a `// REQ-xxx` tag) · (2) SVA traced (an assertion **you confirmed** at Gate 3b) · (3) TC traced (selected at
Gate 4) · (4) simulation passes (all TCs pass, no SVA violation) · (5) mutation score ≥ 0.85, or `N/A` when no mutant can be
generated — `N/A` and `pending` are never evidence of a pass · (6) **you** sign it off in the dashboard (`Ops.sign_req`, the TUI `v` key or `q3tui sign`; never set by the LLM —
with auto approve the *flow* signs, marked as not reviewed; see "Auto approve" below). Gate 5 approved does not mean every REQ signed off. Mutation < 85% locks that REQ's sign-off but
does not block Gate 5.

### Review API (code) — `flows/vlsit/sva/review.py`
`load_properties(engine)`, `review_property(engine, label, status, note)` (confirmed / rejected / pending; rejected →
scoped change request `[sva:<module>] …`), `rtm_rows(engine)`, `sign_req(engine, req_id, signed, note)` (refuses unless
conditions 1–5 hold). `Ops` wraps them (`properties`, `review_property`, `rtm`, `sign_req`) for the TUI keys and the
assistant tools. `flows/vlsit/parse/review.py` and `flows/vlsit/config/review.py` do the same for flagged
requirements and parameters (`confirm_all`).

### Auto approve = approve, confirm, sign
`pipeline.auto_approve`, `pipeline.auto_answer` and `run --yes` approve the gates **and** (VLSIT flow) confirm the pending non-vacuous
assertions (`sva`, at its end) and sign the requirements whose conditions 1–5 hold (`verify`, at its end, only when every test case
passes). Everything done this way is marked `auto-confirmed` / `auto-signed (auto approve), not reviewed` in the reviews and RTM, with a
warning in the log; a locked requirement is never signed. Per-step switches override: `pipeline.step_options: {sva: {auto_confirm:
false}, verify: {auto_sign: false}}` (or the same under `options:` in the flow file). Without auto approve nothing is confirmed or signed
for you (`q3tui confirm-properties`, `q3tui sign --all-ready`, the TUI keys `c`/`C`/`v` are the human way).

### The fix loop and Gate 5
Like the original, a failing test case blocks Gate 5: approving the gate (also an unattended one) never writes `gate_5_approved: true`
over failing test cases, and a question triage cannot settle is blocking. Unlike the original (which stops and asks you per failing
TC), the flow first tries to fix: triage → investigation (when unsure) → scoped fixes → verify again, up to `max_fix_rounds`.

### Update, not re-run
Every step snapshots what it was built from in `step_meta` and, on the next run, re-does only what changed (a module whose
inputs are unchanged keeps its file; an unchanged step sets `ctx.unchanged`). Scoped change requests: `[rtl:<module>]`,
`[sva:<module>]`, `[tb:<tc>]`.

## Formats shared between the steps

- `structured_spec.json` (beyond the schema): `metadata.top_module`; per requirement `sva_hint`, `optional`, `needs_human_decision`,
  `rtl_modules`; top-level `modules: [{name, description, req_ids, instances}]`, `module_order` (leaves first).
  A constraint's `rule` in `final_config.json` is the spec's own (the original schema's `const` pin to one design is dropped).
- Testbench (`tb`): top module `tb_top` (`selected_testplan.metadata.tb_top` overrides), `src/tb/filelist.f` (RTL first, paths relative
  to the project), generated `tb_common.svh` / `tb_tests.svh` / `tb_run.svh`. One simulation prints
  `TC_RESULT <tc_id> PASS|FAIL`, `TC_DETAIL <tc_id> FAIL: <msg>`, `TC_SUMMARY total=<n> failed=<n>`; `+TC=<tc_id>` runs one test case.
- `verify` runs role `run` once per TC with `{tc}` (`{workdir}/simv +TC={tc}`); role `sim` compiles with `{filelist}`, `{top}`, `{workdir}`.
  The optional `mutate` role writes `{workdir}/mutation.json` (format: steps/vlsit/mutation.py docstring); without it the built-in
  mutator runs (`options.mutants_per_module`, default 12). SVA vacuity: `COVER_HIT <label>` lines of the smoke run.
- Gate 3b: approving the gate confirms nothing by itself (`gate_3_approved` only); `review.confirm_all` (Ops "approve all
  properties") confirms the pending non-vacuous ones. Reviews: `schemas/sva_reviews.json`; RTM sign-offs:
  `schemas/rtm_signoffs.json` (both survive reset; written by you, or by auto approve — marked as such).
- `synth_report.json`: `tool`, `pdk`, `top_module`, `synthesis` (pass | fail | not run), `corners{name:{status, cell_count,
  area_estimate_um2, log}}`, `lint{status, tool, warnings, max_warnings}`, `modules{…}`, `req_ids_tagged`, `rule_profile`.

## Regression target
`examples/axi_downscaler/` (spec of `Result/DOWNSCALER_03_09_2026`): same REQ count order of magnitude, same module set,
artifacts validate against the original schemas.

### Answers to config questions are judged by the model

A question the code cannot decide ("Constraint C8 … cannot be evaluated by code. Does it hold?") is answered `yes` (confirmed in code,
no LLM) or in your own words. Your words are read by the `config_overrides` stage, which returns what it overrides / adds as style
rules **and a judgement per constraint**: `confirm` (it holds), `handover` (the answer changes the rule itself: handed to the spec or
requirements step as a decision) or `followup` (a new blocking question with options). Nothing is routed by a fixed question kind.
