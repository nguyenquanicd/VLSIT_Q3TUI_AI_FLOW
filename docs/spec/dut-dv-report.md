# DUT-DV `uvm_vip` backend: final report (V0.1)

## Architecture
`tb` step of the dv flow, optional `backend: uvm_vip` (default `plain_sv` unchanged). Phases, all in `q3tui/flows/dv/tb/uvm/`:

1. **Behavioral** (`spec_facts`, `contract`, `intent`, `review`): code drafts the verification contract from `structured_spec.json`, `final_config.json` and the spec;
   the `tb_intent` LLM stage fills semantic columns; code validates and freezes it by hash. Unspecified behavior stays OPEN/BLOCKED; the review ledger
   separates `user_answered`, `user_accepted_default`, `auto_default`, `pending`.
2. **Architecture and scaffold** (`arch`, `scaffold`, `svgen`): deterministic UVM code with `DV_REGION` markers and `DV_TODO` announcements; regeneration keeps agent/user regions
   and never overwrites hand-edited files.
3. **Semantic** (`worklist`, `semantic`, `lint`): the agent implements the smoke-path regions; code splices, lints and compiles them.
4. **VIP** (`vip`, `binding`, `vipgen`): lexical probe of the existing AXI VIP, binding, adapter and bridge; the VIP is never edited.
5. **RTL integration** (`integrate`): only after the freeze; pyslang reads top ports/parameters; conformance report; wrapper and DUT filelist. Mismatch is reported, never adapted.
6. **Evidence** (`evidence`): VCS compile and run through tool roles; status is judged from logs only.

## Changed files
- New: `q3tui/flows/dv/tb/uvm/` (17 modules), `tb/md/uvm_vip.md`, `tb/rules/vlsit_dut_dv_rule.md` (DUT-DV-001..031), `docs/spec/dut-dv.md`, this report,
  `examples/axi4_sram_ctrl/`, tests `tests/test_dut_dv_{backend,intent,scaffold,vip,semantic,integration,vcs}.py`, `tests/dutdv_rtl.py`, `tests/fixtures/mini_axi_vip/`.
- Edited: `tb/step.py` (backend dispatch), `steps/vlsit/artifacts.py` (5 new schemas), `pyproject.toml` (`vcs` marker), `.gitignore` (example state file).

## Example invocation
```bash
export AXI_VIP_HOME=<axi4_avip @ 86b46171>; module load synopsys/vcs/X-2025.06
cd examples/axi4_sram_ctrl && q3tui run --only tb
Q3TUI_VCS_MODULE=synopsys/vcs/X-2025.06 AXI_VIP_HOME=... uv run pytest -m vcs
```

## Artifact locations (example)
`schemas/` (contract, architecture, VIP integration, DUT integration, review, worklist, validation, evidence written by a run), `src/tb/` (UVM TB, filelists),
`docs/` (review, architecture, conformance reports), `.q3tui/pipeline.json` (what the run was built from).

## Results
- Full regression: 349 passed, 4 failed. The 4 failures predate this work (`examples/sync_fifo/fifo.f` missing): `test_vcs_command`, `test_slang_syntax_adapter`, `test_example_filelist`, `test_example_design`.
- VCS X-2025.06 (`-ntb_opts uvm-ieee-2020-2.0`), real axi4_avip and the real `axi4_sram_ctrl` RTL: compile rc 0; `dut_smoke_test` `status=PASS checks=11 mismatches=0 todo=0`,
  UVM_ERROR 0, UVM_FATAL 0. A deliberately wrong expectation FAILs. The end-to-end example test passes (about 41 s).
- Findings reported, not accommodated: the spec named parameters `PR_*` and the RTL `PARA_*`; the user renamed the spec, no mapping was applied.

## Limitations
Status of every test and rule is in `examples/axi4_sram_ctrl/README.md` and `reports/dv_test_results.md`.
- 29 of 39 regions implemented; COV-001..009 and assertions are `DV_TODO` stubs. SBR-006/007/009 have no check (cycle-level pin properties), so TC-011 (timing) passes without verifying timing and TC-007 (backpressure) is INCOMPLETE. No stress tests.
- Scoreboard rules SBR-004..007 and SBR-009 are not implemented (SBR-001, 002, 003, 008 are; SBR-008 only on observed SRAM cycles, not idle ones). Functional coverage is off. `selected_testplan.json` is not produced, so the sva/verify steps are not wired.
- The VIP drops zero-valued data beats (`ZERO_BEAT_DROPPED`), so checking uses a TB-owned pin monitor. VCS-incompatible VIP parts use stand-ins (strong SVA, FCIRS coverage).
- The generator's parameter-role detection is by name pattern (`ID`, `ADDR`, `DATA`). Python 3.12 is required by the repo.
- Live agent runs cost about $4.3 (intent and semantic stages) plus about $1.6 for the later reruns.

## Next optimizations
1. Add a cycle-level pin checker for SBR-006/007/009 and a way to hold READY low, then re-run TC-007/TC-011.
2. Turn on functional coverage tied to the contract goals, plus the open scoreboard rules.
3. Produce `selected_testplan.json` and connect the sva/verify steps.
4. Generalize the VIP binding beyond axi4_avip (APB/AHB) and drop the stand-ins when the VIP supports VCS.
5. Cache semantic units by input hash across contract revisions to cut agent cost.
