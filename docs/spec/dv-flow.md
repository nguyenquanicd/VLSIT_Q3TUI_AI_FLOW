# The DV flow (`flows/dv/`)

Design verification on its own: a testbench written from the spec, the requirements and the configuration, never from the RTL. Pick it with
`pipeline.flow: dv`. Its front half reuses the [spec flow](spec-flow.md)'s steps (spec, parse, config: code in `flows/spec/`); the `tb` step is the
flow's own (`flows/dv/tb/`, step kind `dv_tb`). The DV work comes from the `tb_env_dev_taidao` branch (the `uvm_vip` backend and the
`axi4_sram_ctrl` example).

```text
spec ─► parse ─► config ─► tb
 human   human     auto    auto   (gate 4: signs itself when its checks pass)
```

## The `tb` step and its two backends

| Backend | Select with | What it does |
|---|---|---|
| `plain_sv` (default) | nothing | A test plan (`schemas/selected_testplan.json`: test cases, each with the REQ ids it checks) and a plain SystemVerilog testbench in `src/tb/` (`tb_top.sv`, one file per test case, `tb_common.svh`). Every requirement must be covered by a selected test case, or the step asks again, then blocks. |
| `uvm_vip` | `pipeline.step_options.tb.backend: uvm_vip` in `q3tui.yaml` | A UVM testbench built from the approved spec and an existing reusable protocol VIP (read only, never generated). The released RTL is integrated as the DUT in a separate phase, compiled and smoke-tested with VCS. Details, phases and rules: [dut-dv.md](dut-dv.md); results: [dut-dv-report.md](dut-dv-report.md). |

```yaml
pipeline:
  flow: dv
  step_options:
    tb:
      backend: uvm_vip
      uvm_vip: {vip: {path: $AXI_VIP_HOME, kind: axi4_avip}, integration: {rtl_dir: src/rtl, top: m_vlsi_axi4_sram}}
```

A worked example with its recorded results is `examples/axi4_sram_ctrl/` (its own tb-only flow `flows/dut_tb.yaml`, kind `dv_tb`).

## Inputs, outputs, folders

| Where | What |
|---|---|
| `spec/`, `schemas/structured_spec.json`, `schemas/final_config.json` | The inputs of `tb`: the spec flow's results (the testbench sees only these). |
| `src/rtl/` | The released RTL, the DUT: read by code only, for the `uvm_vip` integration phase (top, ports, parameters, files). It comes from the [rtl flow](rtl-flow.md). |
| `src/tb/` (`project.tb_dir`) | The testbench: plain SystemVerilog files, or the `uvm_vip` scaffold, integration and tests. |
| `schemas/` | `selected_testplan.json` (`plain_sv`); `golden_manifest.json`, `verification_contract.json`, `tb_architecture.json`, `vip_integration.json`, `dut_integration.json`, `dv_review.json`, `dv_worklist.json`, `dv_evidence.json` (`uvm_vip`); `questions/tb.*` |
| `reports/` | Rendered by code: `tb_architecture.md`, `dv_review_report.md`, `dut_conformance_report.md`, `dv_test_results.md` (`uvm_vip`) |
| `build/sim/` | Compile and simulation logs (`tb_uvm/*.log`) |

## Independence (enforced)

The testbench is written from the spec alone: the RTL is a black box. Every stage of `tb` runs with `deny_dirs` on the RTL (and on `sva/`, the old
model and architecture folders); the RTL stages of the rtl flow, in turn, never see `src/tb/` and `src/sva/`. For `uvm_vip` the RTL is read by
code after the verification contract is frozen; a spec / RTL difference is reported as a conformance finding, never adapted to (rules
DUT-DV-001..031, `flows/dv/tb/rules/vlsit_dut_dv_rule.md`). Run the flow with `session: fresh` (the default): no stage shares a session that has seen the RTL.

## Config and tools

- `tb:` section of `q3tui.yaml`: `max_fix_attempts` (3), `max_turns` (30), `tests_per_agent` (6), `parallel` (4); the TB tab of the settings dialog.
- `project.tb_dir` (default `src/tb`).
- Tool roles (`config/tools.json`, see [flows.md](flows.md) "Tools"): `sim` and `run` (compile and run the testbench: VCS), and for `uvm_vip` `sim_uvm` / `run_uvm`.
  Tests that need real VCS are skipped unless it is set up (`Q3TUI_VCS_MODULE`).

## Gates

`parse` is human (gate 1); `config` and `tb` sign themselves when their checks pass (gate modes: `pipeline.gate_modes`).

## Not in this flow

- Importing testbench / test files into `tb`: the importer sorts them (`core/importer.py` kind `tb`), but the step does not take them yet, so they are reported as not imported.
- `sva`, `verify` and `doc` of the former VLSIT flow stay removed: no assertions step, no RTM / mutation testing, no generated specification document.
