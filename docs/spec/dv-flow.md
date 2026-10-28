# The DV flow (`flows/dv/`)

Design verification on its own: a testbench built from the spec, the requirements and the configuration, never from the RTL (the RTL is a black box DUT until the
testbench is compiled with it). Pick it with `pipeline.flow: dv`. Its front half reuses the [spec flow](spec-flow.md)'s steps (spec, parse, config: code in
`flows/spec/`); `testplan`, `tb`, `prepare` and `simulate` are the flow's own (`flows/dv/<step>/`). The DV work comes from the `tb_env_dev_taidao` branch (the UVM testbench
and the `axi4_sram_ctrl` example).

```text
spec ─► parse ─► config ─► testplan ─► tb ─► prepare ─► simulate
 human   human     auto      human     auto    auto       auto
```

## Two kinds of testbench

The kind is the `testbench` option of the `tb` step (Settings → tb; `auto` by default). The three steps are the same; what each does depends on the kind.

| | `plain` (any IP, no VIP) | `uvm` (an existing protocol VIP) |
|---|---|---|
| `auto` picks it | when no VIP location is set | when a VIP location is set |
| `testplan` | the **test plan**: test cases, each with the REQs it checks; a blocking question for a requirement no test covers (`schemas/test_plan.json`) | the **verification contract**: tests, objectives, scoreboard rules, coverage goals and the open issues you answer, frozen by hash |
| `tb` | `tb_top` and one file per test case in `src/tb/`, compiled with the tool role `sim` against the RTL (fix rounds); `schemas/selected_testplan.json` | architecture, VIP binding, scaffold, the agent's regions; then the RTL as the DUT: conformance, wrapper, compile (`sim_uvm`) |
| `simulate` | every test case runs on its own (`run`, `+TC=<id>`); status from its `TC_RESULT` line | the smoke test and the directed tests (`run_uvm`); status from the UVM report and result line |
| code | `flows/dv/plain/` | `flows/dv/uvm/` ([dut-dv.md](dut-dv.md)) |
| covers | any IP | AXI-style memory-mapped IPs (ID / ADDR / DATA width parameters) so far: another IP is refused with its parameters named |

Both write `schemas/dv_evidence.json` (every run that happened) and `reports/dv_test_results.md`. A worked UVM example with its recorded results is
`examples/axi4_sram_ctrl/` (its own flow `flows/dut_tb.yaml`: the three steps, without the spec flow's).

```yaml
pipeline:
  flow: dv
  step_options:
    tb:      # the options of all three steps (they read them from the tb step)
      testbench: auto       # plain | uvm | auto
      uvm_vip: {vip: {path: $AXI_VIP_HOME, kind: axi4_avip}, integration: {rtl_dir: src/rtl, top: m_vlsi_axi4_sram}}
```

## The `testplan` step (`flows/dv/testplan/`, kind `dv_testplan`): the UVM kind

Builds `schemas/verification_contract.json` (the requirements with their status, the objectives, the scoreboard rules, the coverage goals, the **tests** with the
requirements each checks, the open issues) and `schemas/golden_manifest.json` (the sources the contract was built from), and `reports/dv_review_report.md`
(`schemas/dv_review.json`: who decided each open issue: you, a default you accepted, an automatic default, pending). Unspecified behavior stays an open issue and
a question for you (`questions/testplan.*`); nothing is invented. The gate is human: you review the contract and answer the issues before any testbench is built.
Each open issue has a **scope** (chosen by the stage, kept from run to run; a change request such as `[testplan] OI-004 is a spec gap` reclassifies it): `spec` — the specification is unclear or silent about what the DUT does, so the
answer is a decision about the specification (and the RTL follows it): the question is **→ spec** and the spec step takes the answer; `verification` — a choice of the
testbench alone (how many random vectors, what a check waits for, whether the testbench runs a proof that lives elsewhere, what is not checked in simulation): a design
choice that stays in `testplan` and changes neither the specification nor the RTL. A question the stage asked on its own that only repeats an open issue (the same words,
worded again) is dropped: the issue keeps its permanent ID and you are asked once. A contract frozen before issues had a scope keeps its hash (scope `spec` is the default).
A contract is frozen only when no issue is BLOCKED; the next steps wait for a frozen one. Changes propagate as updates: the contract is made again only when a
source, an answer, a change request or an input option changed. The stage sees the spec, the requirements and the configuration only. The approved standards and
the VIP are options (`uvm_vip.standards`, `uvm_vip.vip`), behavioral inputs of the contract; the integration inputs are not (DUT-DV-026).

## The `tb` step (`flows/dv/tb/`, kind `dv_tb`): the UVM kind

From the frozen contract and the VIP (read only, never generated): `schemas/tb_architecture.json` (`reports/tb_architecture.md`), `schemas/vip_integration.json`
(the probe of the VIP and its binding to the interface contract), the deterministic scaffold in `src/tb/` (`DV_REGION` markers; regeneration keeps the agent's and
your regions and never overwrites hand-edited files), the agent's implementation of the regions (`schemas/dv_worklist.json`, `schemas/dv_validation.json`), compiled
against an interface shell (tool role `sim_uvm`). None of these stages ever sees the RTL (deny_dirs). Then, as the last phase and for the first time, the released
RTL as the DUT (rule DUT-DV-026): read by code for structure and compilation, checked against the frozen interface contract (a difference is a conformance finding in
`schemas/dut_integration.json` and `reports/dut_conformance_report.md`, never adapted to), the wrapper `src/tb/integration/dut_wrapper.sv` and `filelist_dut.f`, and
the compile and elaboration with the RTL. Both compiles are recorded in `schemas/dv_tb_evidence.json`; the shell compile is "not a DUT result". The RTL is an input of
the step: a changed RTL re-runs `tb` and `simulate` and not `testplan`.

## The `simulate` step (`flows/dv/simulate/`, kind `dv_simulate`): the UVM kind

The smoke test (`uvm_vip.integration.smoke_test`) and, with `uvm_vip.semantic.scope: all`, every directed test whose body the agent implemented, run with the tool
role `run_uvm` on the simv `tb` built. A status is `PASS` only when the simulator returned 0, the log shows zero UVM_ERROR and UVM_FATAL, the result line says PASS
and no DV_TODO was announced; a test that ran clean but made no check is `INCOMPLETE`, never a PASS. `schemas/dv_evidence.json` records every run that happened;
`reports/dv_test_results.md` lists the tests, with the ones not run (`todo`). No LLM stage. A failure is a finding about the DUT or the testbench, never a reason to
change the expected behavior.

## The plain kind (`flows/dv/plain/`)

- **`testplan`**: an LLM stage (`tb_plan`, the spec, the requirements and the configuration only) plans the test cases; code numbers them (`TC-001` …), drops unknown REQ ids,
  applies conditional test cases by parameter, checks the REQ coverage and asks again for an uncovered requirement, then raises a blocking question (accept it as not
  simulated, or add a test case). Static / assumption requirements are not simulation coverage. A requirement no simulation of the DUT can check (a frequency goal, a lint or
  coding rule, an SDC / synthesis constraint, a document) is **left out by the planner with its reason** (`excluded` in `test_plan.json`, status `excluded` on the Requirements tab):
  it is not uncovered and nothing is asked about it: you review the list with the plan. The gate is human: you review the plan before any file is written; a change
  request or an answer updates it, and `tb` rewrites only the test cases that changed (a scoped change for one stays `[tb:TC-002] …`).
- **`tb`**: code writes the result macros, the test includes and the runner; the LLM writes `tb_top.sv` and, in groups, the test case files (only its own files); code checks them
  (`task automatic <name>()`, a `` `TC_CHECK ``, the `// TC-001 | REQ-…` first line) and compiles with the tool role `sim` against `src/rtl` (a missing RTL skips the compile, and
  `simulate` then waits). An error inside the RTL is not fixed by the testbench: it is a question. `run.sh` and `filelist.f` are generated.
- **`simulate`**: each selected test case runs with `+TC=<id>`; `TC_RESULT <id> PASS|FAIL` (and `TC_DETAIL <id> FAIL: <msg>` for each failed check) is read from the log. No result line is a
  FAIL, never a guess. A failure is a finding about the DUT or the testbench, never a reason to change the expected behavior. A failed test whose log names an input file
  that exists nowhere in the project (a program list, a hex image, a golden-model export; `plain/simulate.missing_inputs`, by code) is **BLOCKED**: it is not triaged, nothing is
  handed back (the testbench cannot make those files), the step warns with the files and `reports/dv_test_results.md` lists it. Blocked tests alone do not fail the step; their
  other checks are judged once the inputs exist. Only the testbench's own failure lines (`TC_DETAIL`) name a missing file when the log has them: a file the
  testbench probes and goes on without (an optional `.xskip`, the simulator's "Cannot open file" warning) is not one. A test killed at the time limit of the
  `run` tool (`timeout_s` in config/tools.json) is **TIMEOUT**: not judged, not triaged, a warning to raise the limit; the other tests still run (a `run` tool
  that fails without a readable error still stops the step). The tests run in parallel, `tb.sim_parallel` at once (Settings → TB "Simulations at
  once"; empty = 80 % of the CPU cores; each run holds a simulator license); the results, the evidence and the report keep the plan's order.
  `schemas/dv_test_results.json` has every selected test while they run (PENDING, RUNNING, then its result; `last`: its result in the run before; `est_s`: its
  time then, the evidence's when there is no run before; `jobs`, `order`: the slots and the start order, for the time left), rewritten as each
  test starts and ends: the **Tests** tab is live. The evidence of the run before stays until the tests are over (the TUI shows it meanwhile). A run that is
  stopped marks its unfinished tests NOT_RUN (a killed one: the TUI does, once its process is gone).
  **After a fix, the tests that did not pass in the run before run first** (FAIL and BLOCKED in its `dv_test_results.json`; a test that run did not finish counts with its result before: `plain/simulate.bad_before`); the others take the
  slots that are left. When one of them that was FAIL fails again (its fix did not work), the tests that did not fail before stop (the running simulators are
  killed through `run_role(…, stop=)`, the others are not started: NOT_RUN, no evidence, in the report) while the ones that failed before finish, so the
  same triage gets all of them; the fix loop goes on. A cancelled run (Ctrl-C, `x` in the TUI) kills its simulators the same way instead of waiting for them.

## Test programs: the `prepare` step (`flows/dv/prepare/`, kind `dv_prepare`)

A test that loads software or data (an instruction / data memory image, a program list, the expected results of the ISA simulator, a model's golden export) needs
files the testbench cannot make. `prepare` makes them, after `tb` (it reads the testbench to learn what each test loads, from which folder, in which format) and
before `simulate`; the plain kind only (a UVM testbench loads no programs).

- **The LLM** (stage `prepare`, `md/skill.md` section SYSTEM) sees the spec, the requirements, the test plan and the testbench, never the RTL (`deny_dirs`: `src/rtl`,
  `src/sva`, `build/`). It writes `src/sw/` only: sources (assembly, C, start-up code, linker scripts), scripts, and a `Makefile`. It may read and reuse a riscv-tests
  checkout (`~/.q3tui/tools/riscv-tests`, given as an extra folder) or write anything from scratch. It builds with the in-process tool `build`: code runs the tool role
  `sw_build` (`make -C {src} PROG_DIR={out} RISCV_PREFIX={riscv_prefix} SPIKE={spike}`) and answers `clean` or the log's end. Its answer (`PrepareResult` in
  `flows/rtl/lib/models.py`): `prog_dir` (under `build/`, the testbench's default folder) and, per test, the files it reads; files only you can provide are `external`, with a question.
- **Code** builds once more and checks every file exists; a missing one gets a small fix session (`prepare_fix<n>`, the build log and the missing files, up to
  `rtl.max_fix_attempts`). `schemas/sw_manifest.json`: per test `ready` / `missing` / `external` / `not built`, the toolchain used, the problems. The TUI tab **Programs** shows it.
- **Tools** (`prepare/toolchain.py`): a RISC-V GCC (`riscv64-unknown-elf-gcc`, `riscv32-unknown-elf-gcc` or `riscv-none-elf-gcc`), Spike and the `dtc` it runs, and `make`.
  Configured roles win (`riscv_gcc`, `iss`, `sw_build` in `config/tools.json`); otherwise they are looked for in `$RISCV/bin`, on `PATH` and in `~/.q3tui/tools`
  (`$Q3TUI_HOME/tools`); dtc also next to Spike. Nothing is downloaded during a run: without the tools the LLM still writes the sources, the step warns and every
  test is `not built`. `q3tui act riscv_tools_setup` writes what it finds into `config/tools.json` (a role already there is kept); `install=yes` first puts what is
  missing into `~/.q3tui/tools` (the xPack GCC download; Spike and dtc built from source; riscv-tests cloned).
- **Update, not re-run**: the step records the hashes of its inputs; with sources already there it lists what changed and asks for only that. Change requests
  scoped to a test (`[prepare:TC-004] …`) come from `simulate`.

**Blocked tests** (`plain/simulate.py`): a failed test whose log names an input file that exists nowhere in the project is BLOCKED, by code, and never triaged.
When the flow has `prepare` and its tools, `simulate` hands the missing files to it (a change request scoped to the test, the fix loop runs prepare and simulate
again; the same test at most `pipeline.escalate_after_repeats` times, then a warning). Files prepare listed as only yours, or a prepare without tools: a warning only.

## A failing test is judged (`flows/dv/triage.py`)

`simulate` does not stop at a failing test. A second LLM stage (`dv_triage`, a fresh read-only session that may read the RTL, which the testbench stages may not) decides whose fault each failure is, against the
spec, by the playbook skill `sim-triage` (`flows/dv/skills/`; yours in `config/skills/` wins, as for the rtl flow's `smoke-triage` this is modelled on), and code dispatches the verdict, the way the rtl flow's smoke test does:

| Verdict | What happens |
|---|---|
| `tb_bug` | a change request for `tb`, scoped to that test (`[tb:TC-019] …`; the UVM kind: the whole step). `simulate` marks its loop: the engine updates `tb`, then runs `simulate` again |
| `rtl_bug` | a change request for the `rtl` step of the flow that owns the RTL (`handoff_flow`, default `rtl`), scoped to the module (`[rtl:<module>] …`). In an unattended run (`pipeline.auto_approve` / `auto_answer`, or `--yes`) the fix loop runs that flow within the run (`engine.hand_to_flow`, same process, lock and event log), then `tb` and `simulate` on the new RTL; bounded by the fix rounds, a fix that keeps coming back (`escalate_after_repeats`, counted only once the RTL changed after it) becomes a question, and a round after which the RTL did not change or the failures did not go down stops with the hand-off for you. Otherwise `simulate` stays failed until you run that flow |
| `spec_issue` / `unsure` | a blocking question for you (`spec_gap` / `design_choice`); nothing is changed |

A module a generator made (the rtl flow's `csr` step: `src/rtl/csr/provided.json`) is named in the triage prompt ("Generated RTL"): a bug in it is an `rtl_bug`
like any other (the `rtl` step changes it in place, and the change is kept as a patch that the `csr` step applies again when it generates the module again:
[rtl-flow.md](rtl-flow.md) "Generated RTL: changes as patches"), unless your spec forbids editing it or the rtl option `keep_generated` is on: then it is a
`spec_issue` whose proposal names the choices (fix the generator, allow the change in place, revise the spec to the generated behaviour).

The rounds are bounded (`max_rounds`, else `pipeline.max_fix_iterations`) and counted until the tests pass, you regenerate, or you answer. A fix handed over and coming back again after
`pipeline.escalate_after_repeats` rounds becomes a question, not another round. The testbench is never changed to agree with the RTL: the spec is the reference. Every verdict is in
`schemas/dv_triage.json` (the **Triage** tab). When the triage cannot run (no credentials), the failure is reported as it was.

## Inputs, outputs, folders

| Where | What |
|---|---|
| `spec/`, `schemas/structured_spec.json`, `schemas/final_config.json` | The inputs of `testplan`: the spec flow's results (the testbench sees only these). |
| `src/rtl/` | The released RTL, the DUT: read by code only, by the last phase of `tb` (top, ports, parameters, files). It comes from the [rtl flow](rtl-flow.md). |
| `src/tb/` (`project.tb_dir`) | The testbench: plain SystemVerilog files (`tb_top.sv`, `tests/`, `run.sh`), or the UVM scaffold, the integration (wrapper, `filelist_dut.f`) and the tests. |
| `schemas/` | plain: `test_plan.json` (`testplan`), `selected_testplan.json` (`tb`). UVM: `testplan`: `golden_manifest.json`, `verification_contract.json`, `dv_review.json`, `questions/testplan.*`. `tb`: `tb_architecture.json`, `vip_integration.json`, `dv_worklist.json`, `dv_validation.json`, `dut_integration.json`, `dv_tb_evidence.json`. `prepare`: `sw_manifest.json`. `simulate`: `dv_evidence.json`, `dv_triage.json`, `dv_test_results.json` (plain: every test's status, live). |
| `reports/` | Rendered by code: `dv_review_report.md` (`testplan`); `tb_architecture.md`, `dut_conformance_report.md` (`tb`); `dv_test_results.md` (`simulate`). |
| `src/sw/` | The test programs `prepare` writes (sources, scripts, Makefile); built into its `prog_dir` under `build/` (`build/sw/sw_build.log`). |
| `build/sim/` | Compile and simulation logs (`tb_uvm/*.log`) |

## Independence (enforced)

The testbench is built from the spec alone: the RTL is a black box. Every LLM stage of the flow runs with `deny_dirs` on the RTL (and on `sva/`, the old model and
architecture folders); the RTL stages of the rtl flow, in turn, never see `src/tb/` and `src/sva/`. The RTL is read by code after the verification contract is frozen,
in the last phase of `tb`; a spec / RTL difference is reported as a conformance finding, never adapted to (rules DUT-DV-001..031,
`flows/dv/tb/rules/vlsit_dut_dv_rule.md`). Run the flow with `session: fresh` (the default): no stage shares a session that has seen the RTL.

## Config and tools

- **Settings → `tb`** (the flow's add-on, `flows/dv/addon.py`; also the assistant): testbench kind, VIP location (optional), VIP kind, VIP revision, VIP environment variable, RTL folder,
  DUT top module, smoke test, semantic scope. They are saved flat to `pipeline.step_options.tb` in `q3tui.yaml` (`testbench`, `vip_path`, `vip_kind`, `vip_revision`, `vip_env`,
  `rtl_dir`, `top`, `smoke_test`, `scope`). **The VIP is optional**: a simple IP has no protocol interface to drive with one. `testplan` never waits for it (it only tells the contract stage whether a VIP is there: with none,
  no interface gets the stimulus agent `vip_master`). `tb` uses it when it is set (a VIP that is set must exist) and waits, naming the interfaces, only when the contract has an interface
  driven by a protocol VIP and none is set.
- `pipeline.step_options.tb.uvm_vip` (the nested group, for what the dialog does not have): `vip` (path, kind, revision, env, config), `standards`, `integration`
  (`rtl_dir`, `top`, `incdirs`, `defines`, `simulator`, `smoke_test`, `param_map`, `port_map`), `semantic.scope` (`smoke` | `all`). The flat options are laid over the
  group (a value set in the dialog wins).
- `tb:` section of `q3tui.yaml`: `max_fix_attempts` (3), `max_turns` (30), `parallel` (4); the TB tab of the settings dialog. `project.tb_dir` (default `src/tb`).
- Tool roles (`config/tools.json`, see [flows.md](flows.md) "Tools"): plain: `sim` (compile `tb_top` with the RTL; its log is read by the `parse` of the role) and `run` (run the simv with `+TC={tc}`); UVM: `sim_uvm` (compile with the VIP and the RTL) and `run_uvm` (run one UVM test): VCS; `prepare`: `sw_build` (make), `riscv_gcc`, `iss` (Spike), see above. `q3tui tools init`
  writes templates. Tests that need real VCS are skipped unless it is set up (`Q3TUI_VCS_MODULE`).

## TUI (`flows/dv/panels.py`)

Read-only tables over what the steps wrote, before the usual Files / Questions tabs. The hint above a table turns into a warning when something needs you.

| Step | Tab | Rows (from) | Warns when |
|---|---|---|---|
| `testplan` | **Tests** | each test: id, name, kind, number of REQs, description (the contract's tests, or the plain test plan); the detail pane has the REQs |  – |
| `testplan` | **Requirements** | each requirement: category, status (planned / open / blocked / not_observable / …), the tests that check it (the contract's, or the spec's with the plan's coverage) | a requirement is open, blocked or draft (answer on the Questions tab) |
| `tb` | **Test cases** | plain: each test case: selected, compile ✔ / ✘, file (`selected_testplan.json`) | a selected test case does not compile |
| `tb` | **Regions** | each region of the testbench the agent (or you) implement: kind, owner, done (`dv_worklist.json`) | regions are still open |
| `tb` | **Conformance** | where the released RTL differs from the contract, and the validation findings (`dut_integration.json`, `dv_validation.json`) | a finding has severity error |
| `prepare` | **Programs** | each test that loads files: ready / missing / external / not built, the files (`sw_manifest.json`) | a test's files are not ready |
| `simulate` | **Tests** | plain: every selected test: PENDING / RUNNING (with its time so far, yellow past 1.5× its estimate) / PASS / FAIL / TIMEOUT / BLOCKED / ERROR / NOT_RUN, the estimate (**Est.**: its time in the run before), the result of the run before, the note; a line with the counts and the time left (the estimates of the running and pending tests on the run's slots, in their start order; a test without an estimate counts as their median) (`dv_test_results.json`, rewritten as each test starts and ends; the plan's tests not run yet: `–`) | a test did not pass |
| `simulate` | **Evidence** | each run: status read from the log, UVM_ERROR / UVM_FATAL, log (`dv_evidence.json`) | a run is not PASS |
| `simulate` | **Triage** | each judged failure: verdict, where it goes, RTL module, summary (`dv_triage.json`) | an RTL bug, a spec issue or an unsure verdict waits for someone |

The tables are empty (with a note on what writes them) until the step has run.

## Gates

`parse` and `testplan` are human (gate 1, and the review of the contract); `config`, `tb`, `prepare` and `simulate` sign themselves when their checks pass (gate modes:
`pipeline.gate_modes`).

## Not in this flow

- Importing testbench / test files: the importer sorts them (`core/importer.py` kind `tb`), but the flow does not take them yet, so they are reported as not imported.
- Formal, functional coverage closure, `sva`, `verify` and `doc` of the former VLSIT flow: no assertions step, no RTM / mutation testing, no generated specification
  document.
