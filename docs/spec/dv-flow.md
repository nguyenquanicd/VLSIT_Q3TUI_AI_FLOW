# The DV flow (`flows/dv/`)

Design verification on its own: a testbench built from the spec, the requirements and the configuration, never from the RTL (the RTL is a black box DUT until the
testbench is compiled with it). Pick it with `pipeline.flow: dv`. Its front half reuses the [spec flow](spec-flow.md)'s steps (spec, parse, config: code in
`flows/spec/`); `testplan`, `tb` and `simulate` are the flow's own (`flows/dv/<step>/`). The DV work comes from the `tb_env_dev_taidao` branch (the UVM testbench
and the `axi4_sram_ctrl` example).

```text
spec ─► parse ─► config ─► testplan ─► tb ─► simulate
 human   human     auto      human     auto    auto
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
  FAIL, never a guess. A failure is a finding about the DUT or the testbench, never a reason to change the expected behavior.

## Inputs, outputs, folders

| Where | What |
|---|---|
| `spec/`, `schemas/structured_spec.json`, `schemas/final_config.json` | The inputs of `testplan`: the spec flow's results (the testbench sees only these). |
| `src/rtl/` | The released RTL, the DUT: read by code only, by the last phase of `tb` (top, ports, parameters, files). It comes from the [rtl flow](rtl-flow.md). |
| `src/tb/` (`project.tb_dir`) | The testbench: plain SystemVerilog files (`tb_top.sv`, `tests/`, `run.sh`), or the UVM scaffold, the integration (wrapper, `filelist_dut.f`) and the tests. |
| `schemas/` | plain: `test_plan.json` (`testplan`), `selected_testplan.json` (`tb`). UVM: `testplan`: `golden_manifest.json`, `verification_contract.json`, `dv_review.json`, `questions/testplan.*`. `tb`: `tb_architecture.json`, `vip_integration.json`, `dv_worklist.json`, `dv_validation.json`, `dut_integration.json`, `dv_tb_evidence.json`. `simulate`: `dv_evidence.json`. |
| `reports/` | Rendered by code: `dv_review_report.md` (`testplan`); `tb_architecture.md`, `dut_conformance_report.md` (`tb`); `dv_test_results.md` (`simulate`). |
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
- Tool roles (`config/tools.json`, see [flows.md](flows.md) "Tools"): plain: `sim` (compile `tb_top` with the RTL; its log is read by the `parse` of the role) and `run` (run the simv with `+TC={tc}`); UVM: `sim_uvm` (compile with the VIP and the RTL) and `run_uvm` (run one UVM test): VCS. `q3tui tools init`
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
| `simulate` | **Evidence** | each run: status read from the log, UVM_ERROR / UVM_FATAL, log (`dv_evidence.json`) | a run is not PASS |

The tables are empty (with a note on what writes them) until the step has run.

## Gates

`parse` and `testplan` are human (gate 1, and the review of the contract); `config`, `tb` and `simulate` sign themselves when their checks pass (gate modes:
`pipeline.gate_modes`).

## Not in this flow

- Importing testbench / test files: the importer sorts them (`core/importer.py` kind `tb`), but the flow does not take them yet, so they are reported as not imported.
- Formal, functional coverage closure, `sva`, `verify` and `doc` of the former VLSIT flow: no assertions step, no RTM / mutation testing, no generated specification
  document.
