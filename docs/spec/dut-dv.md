# DUT design verification: the `uvm_vip` testbench backend

Optional backend of the VLSIT `tb` step: a UVM DUT testbench built from the approved specification and an existing reusable
protocol VIP. The default backend, `plain_sv` (docs/spec/vlsit-flow.md), is unchanged. Rules: `q3tui/flows/vlsit/tb/rules/vlsit_dut_dv_rule.md`
(DUT-DV-001..031). Worked example: `examples/axi4_sram_ctrl/`.

**Golden source = the approved specification (and approved standards). RTL = the DUT only.** The RTL is read by code in a separate
integration phase, after the verification contract is frozen, for structure and compilation only.

## Selecting it

```yaml
pipeline:
  session: fresh            # the testbench stages must not share a session that has seen the RTL
  step_options:
    tb:
      backend: uvm_vip      # plain_sv (default) | uvm_vip
      uvm_vip:
        standards: []                     # behavioral: approved standard documents
        vip: {path: $AXI_VIP_HOME, kind: axi4_avip, revision: <commit>, config: {}}   # the existing VIP (read-only)
        integration: {rtl_dir: src/rtl, top: null, incdirs: [], defines: {}, simulator: vcs, smoke_test: dut_smoke_test}
```

Input classes (`q3tui.flows.vlsit.tb.uvm.INPUT_CLASSES`): *behavioral* (spec documents, `structured_spec.json`, `final_config.json`,
`standards`) may define expected behavior; *vip* describes the reusable component; *integration* never defines behavior.
`behavioral_fingerprint` leaves the integration inputs out, so changing the RTL location, defines or include directories never makes
the behavioral phase stale.

## Status

| Milestone | Content | State |
|---|---|---|
| M1 | rule file, option model, backend switch, example layout | done |
| M2 | specification to verification contract and TB architecture | done (fake-LLM tests; no live LLM run yet) |
| M3 | deterministic UVM scaffold | done (VCS compile and smoke run checked on the interface shell; slang type-check) |
| M4 | existing AXI VIP probe, binding and adapter | done (real axi4_avip probed; VCS compile and elaboration of the VIP-bound testbench against the interface shell) |
| M5 | spec-driven scoreboard and smoke test | done (one live agent run; compile PASS with VCS; run against the interface shell hits the watchdog by design) |
| M6 | RTL integration and conformance | done (real RTL: 6 parameter-name mismatches reported; with an approved mapping the interface conforms) |
| M7 | VCS compile with the RTL as DUT | done: the specification now names its parameters `PARA_*` like the RTL, so no mapping is needed; conformance clean, VCS compile rc 0 |
| M8 | one smoke simulation | done: `dut_smoke_test` with the real RTL, `status=PASS checks=8 mismatches=0 todo=0`, UVM_ERROR 0, UVM_FATAL 0 (a deliberately wrong expectation FAILs) |
| M9 | example regression | done: the committed example runs end to end with VCS on a copy (`-m vcs` test), smoke test PASS, no LLM beyond the reviewed intent |
| M10 | final documentation and report | done: `docs/spec/dut-dv-report.md` |

## M2: from the approved specification to a frozen contract

Inputs (behavioral only): the spec documents, `schemas/structured_spec.json` (Gate 1) and `schemas/final_config.json` (Gate 2); both must be
`approved` (DUT-DV-001). Code (`tb/uvm/spec_facts.py`, `contract.py`) reads the port tables, finds every place the specification marks an
assumption, flags requirements that may restate one (`ASSUMED_CANDIDATE`) and requirements that talk about the inside of the design
(`observability_hint: INTERNAL`), and writes the draft contract. The `tb_intent` stage (LLM, read-only on `spec/`, `deny_dirs` = rtl, sva, tb,
schemas) decides each requirement (observability, origin, status), the interfaces, objectives (VO), scoreboard rules (SBR), coverage (COV),
tests (TC) and open issues (OI). Code (`intent.py`) applies it, derives the back-links, and refuses: an undecided assumption candidate, an assumed
behavior that is not open/blocked, an internal requirement planned for a test, a test/rule/goal without a trace, dangling or dropped permanent
IDs, a missing smoke test. A clean contract is frozen by hash; `tb_architecture.json` (+ `docs/tb_architecture.md`) is derived from it by code.
BLOCKED open issues become blocking questions and the contract stays a draft.

| Artifact (schemas/) | Content |
|---|---|
| `golden_manifest.json` | pinned sources (path, SHA-256, approval) and the inputs excluded as non-behavioral |
| `verification_contract.json` | interface contract, requirements with their decisions, VO/SBR/COV/TC, open issues, `contract_hash` |
| `tb_architecture.json` | components, interfaces and their drivers, tests, build; VIP binding and RTL integration `pending` |

## Review ledger and report

Every open issue is a question with a stable ID (`Q-TB-<issue id>`, e.g. `Q-TB-OI-006`; issue IDs are permanent). `schemas/dv_review.json` and
`docs/dv_review_report.md` say who decided each one: `user_answered` (an answer to the question), `user_accepted_default` (a review decision in
`schemas/dv_review_decisions.json`, bound to the issue text by `issue_sha`), `auto_default` (default accepted by a gate in auto mode or --yes, no
review) or `pending`. A reviewed issue keeps its wording: the model may only resolve it; if the text ever changes the decision no longer counts
and the issue is pending again. An answered issue must be applied in the contract (`resolved` + `resolution`). The report is rewritten at every run
and at gate approval.

## M3: the UVM scaffold

Code only (`tb/uvm/scaffold.py`); no RTL, no VIP yet. Written to `src/tb/` from the frozen contract: `dut_tb_params_pkg` (approved parameters),
`dut_pins_if` (the specification's port names and widths), `integration/dut_wrapper_shell.sv` (an **interface shell with outputs tied to 0, not the DUT**,
so the testbench compiles before integration; the integration phase replaces it), `dut_dv_pkg` with the transactions (VIP independent), the SRAM
agent skeleton (monitor, responder), the VIP adapter slot, the scoreboard (one check function per SBR rule with its requirement IDs and spec source),
the coverage subscriber (one covergroup per COV), sequences, tests (`TC-nnn | REQ` trace; the smoke test class is `integration.smoke_test`), the
assertion bind point, `tb_top`, `filelist_tb.f`.

- **Regions.** Semantic work is a `DV_REGION(<id>) BEGIN scaffold ... END` block with a `DV_TODO(<id>, ...)` call that announces itself at run time
  (UVM_WARNING `[DV_TODO]`, counted). Change `BEGIN scaffold` to `BEGIN agent` / `BEGIN user` when you implement it: the region is kept on regeneration.
  A file changed outside its regions is never overwritten (the new scaffold goes to `<file>.scaffold_new`).
- **Result line.** The base test prints `VLSIT_DUT_TB_RESULT test= tc= status= checks= mismatches= todo=` and `TC_RESULT <tc> <status>`. `PASS` needs no
  UVM_ERROR/FATAL, no mismatch, no DV_TODO announced and at least one check; otherwise `INCOMPLETE` (or `FAIL`).
- **Worklist and validators.** `schemas/dv_worklist.json` lists every region with its requirement context. `schemas/dv_validation.json`: required files,
  filelist entries exist, TC/SBR/COV traces match the contract (no dangling, none dropped), no hierarchical DUT reference outside `integration/`
  (DUT-DV-028), no tabs, scaffold-owned regions still stubs, VIP package names only in the adapter (DUT-DV-019).
- **Evidence.** `schemas/dv_evidence.json` records only commands that ran. The shell compile (tool role `sim_uvm`) is entered as a compile of the testbench
  against the interface shell: "not a DUT result". Without the role it is `NOT_RUN` and nothing is recorded.
- **Tests.** `tests/test_dut_dv_scaffold.py` (deterministic). `tests/test_dut_dv_vcs.py` runs real VCS and skips unless `Q3TUI_VCS_MODULE` is set:
  `Q3TUI_VCS_MODULE=synopsys/vcs/X-2025.06 uv run pytest -m vcs`.

## M4: the existing VIP

`tb/uvm/vip.py` probes the VIP's own source (lexical, read-only; DUT-DV-010): packages and the compile closure for the roles the testbench needs,
interfaces and BFM ports, classes by UVM role, the agent's config and sequencer members, the item fields and enums, analysis ports, `config_db` keys,
the configuration constants and mode macros, and detectors for known VIP defects. `binding.py` derives the binding to the interface contract: the
signal map by AXI name (direction and width checked with the approved configuration), the VIP configuration the DUT needs, ties, the observation
choice, and the substitutions below. Result: `schemas/vip_integration.json` (portable: the VIP path is the expression of the options, e.g. `$AXI_VIP_HOME`).
`vipgen.py` generates the derived constants package, the bridge, the adapter, the `tb_top` block and the filelist.

- **The VIP is never edited** (DUT-DV-020). Where its configuration constants differ from the approved configuration (axi4_avip: `DATA_WIDTH` 512, the
  DUT needs 32) a derived copy of that one package is generated in `src/tb/vip_cfg/` and used instead of the VIP's own in the filelist.
- **Only the adapter knows the VIP** (DUT-DV-019). `dut_axi_adapter.send(dut_axi_txn)` creates the VIP master agent and drives one transfer through the VIP
  sequencer (non-outstanding, one at a time). The lint refuses a VIP package or class name anywhere else.
- **Observation does not use the VIP monitor.** The probe found that the axi4_avip item converter drops data beats whose value is 0 (`ZERO_BEAT_DROPPED`,
  `axi4_master_seq_item_converter.sv:84`), so the checker observes the DUT pins with the testbench-owned `dut_axi_pin_monitor` (AXI handshakes only).
- **What the simulator rejects is substituted and disclosed** (observed with VCS X-2025.06, each in the binding findings): the VIP's `master_assertions`
  (strong SVA operators, `Error-[SVA-SONS]`) by a unit with the same ports and no properties; the VIP's `axi4_master_coverage` (run-time
  `Error-[FCIRS]` on its covergroup, not suppressible) by a stand-in class found first through the include path; `-suppress=ETMFCB` for a compile-time enum
  check. The VIP's own protocol assertions and coverage are therefore NOT_RUN; the DUT is checked by the scoreboard and spec-derived assertions.
  An active agent also needs `has_coverage = 1` (otherwise its connect phase dereferences a null component): `ACTIVE_NEEDS_COVERAGE`.
- **Tests.** `tests/test_dut_dv_vip.py` (a small stand-in VIP in `tests/fixtures/mini_axi_vip`; the real VIP when `AXI_VIP_HOME` is set) and the VCS test
  (`Q3TUI_VCS_MODULE` and `AXI_VIP_HOME`).

## M5: the agent implements the smoke path

Code never writes semantic SystemVerilog. `tb/uvm/semantic.py` gives the agent the regions on the smoke path (the SRAM monitor and responder, `seq.smoke`,
the scoreboard rules the smoke test relies on and their memory-side hooks), with their requirements, specification sources and the user's decisions
(`dv_review`), and the fixed files as context. The stage (`tb_semantic_<unit>`, read-only tools, `deny_dirs` = rtl, sva, schemas) returns the region
bodies as data. Code splices them, sets the region owner to `agent` (so they are distinguishable from the scaffold and kept on regeneration), refuses
`DV_TODO`, `$finish/$display`, `#` delays, `$random`, `force`, hierarchical DUT references and VIP names, then compiles through the tool role `sim_uvm`:
compiler errors in testbench files go back to the agent (fix rounds, `tb.max_fix_attempts`). An unchanged unit is not redone (input hash).

- A scoreboard rule runs only when the test enables it (`rules` of the test class, from `rule_ids` of the contract): an unimplemented rule outside the
  smoke path does not block the smoke result. Coverage is off by default (`has_coverage = 0`) until its regions are implemented.
- The base test has a watchdog (`+DV_TIMEOUT_NS`, default 1 ms): a run without progress ends with a `UVM_FATAL`, which is a FAIL, never a PASS.
- Result of the first live run on axi4_sram_ctrl (claude-sonnet-5-5): 4 agent calls, about 4.3 USD; two compile attempts (the first failed, the second
  after a fix round passed). Open regions: 29 of 39. Known gap reported by the agent: `o_sram_wstrb` = 0 while `o_sram_we` = 0 (SBR-008) is not checked,
  because the SRAM monitor only builds transactions for active cycles.
- Against the interface shell (no DUT) the smoke test cannot make progress: the watchdog fires (`UVM_FATAL`), as designed. The DUT is integrated in M6.

## M6: the RTL phase

Only after the contract and the architecture are frozen (`integration_phase`, `tb/uvm/integrate.py`; no LLM, no RTL text goes to a prompt). Code reads the
RTL with pyslang for **structure and compilation only** (DUT-DV-004): the top module, its ports and parameters, elaboration diagnostics and the file list
(`rtl_dir/filelist.f`). `schemas/dut_integration.json` records what was read and why, the hashes of the frozen inputs, the mappings, and the conformance
findings; `docs/dut_conformance_report.md` is the readable report.

- **Conformance** against the interface contract: top name, every port (presence, direction, total width evaluated with the approved parameters, packed
  shape), extra RTL ports (an extra input is an error), parameters. Any error stops the step with the report and writes no wrapper. **Nothing is adapted
  silently** (DUT-DV-005, 030): a rename needs an approved mapping in the options (`integration.param_map`, `integration.port_map`). When the specification
  itself lists the original names (its `Original name` column) the report shows them as a suggestion; they are not applied.
- On conformance: `integration/dut_wrapper.sv` (the RTL top with the approved parameters, ports connected by name: wires only; the only place with the DUT
  instance name) and `filelist_dut.f` (the testbench list with the RTL and the real wrapper in place of the shell). The frozen artifacts are checked
  unchanged afterwards, and the testbench is compiled through the tool role `sim_uvm` (evidence entry `tb + VIP + RTL (DUT)`).
- Result on `axi4_sram_ctrl` (the Design flow's RTL, copied to `examples/axi4_sram_ctrl/rtl/`): all 37 ports conform; the six approved parameters
  `PR_*` are `PARA_*` in the RTL (the specification lists those original names). Without a mapping the step stops with that report. A scratch run with
  `param_map` set (not approved) conformed and compiled with VCS (rc 0, VIP + testbench + RTL).

## M7/M8: compile and smoke run with the real DUT

- The specification parameters were renamed `PR_*` to `PARA_*` (the user's decision, after the conformance report); the generator derives the width
  roles (`@ID_WD@`, `@ADDR_WD@`, `@DATA_WD@`) from the parameter names, so it is not tied to one spelling. The recorded answer to Q-TB-OI-006 keeps its original wording.
- Reset sequence in `tb_top`: idle for 3 negedges, assert for `reset_cycles` negedges, release. The VIP drives X on VALID/READY before reset, so the
  bridge turns those into 0 (`=== 1'b1`).
- `smoke_phase` runs the smoke test through the tool role `run_uvm`; `judge_run` reads only the log (DUT-DV-022/023). Evidence keeps the earlier FAIL runs.
- `uvm_vip.vip.env` names an environment variable (here `AXI_VIP_HOME`) used for the VIP root in every generated filelist, so nothing machine specific is
  written. A second run keeps the integration files (their hashes stay in the scaffold record) and creates no `.scaffold_new`.
- Not covered yet: SBR-008 on idle SRAM cycles, functional coverage (off), the rules SBR-004..007 and SBR-009, 29 regions are still `DV_TODO` stubs, `selected_testplan.json` is not produced.

## M9: the example as a regression

`examples/axi4_sram_ctrl/` is a finished run (`src/tb`, `.q3tui/pipeline.json`, reports). `tests/test_dut_dv_vcs.py::test_example_runs_end_to_end_with_vcs_and_the_smoke_test_passes`
copies it, runs the `tb` step with only `tb_intent` answered (from the reviewed contract; any other LLM stage fails the test), and checks a `PASS` run
read from the log with zero UVM_ERROR/UVM_FATAL and that no generated file changed. It found a real defect: a regeneration put the scaffold stubs back
over agent-implemented regions when the file matched its recorded hash; `scaffold.write` now always keeps agent/user regions.
