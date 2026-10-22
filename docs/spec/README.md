# Q3TUI — Design Specification

Q3TUI takes a hardware block from **intent to checked RTL**. A pipeline engine runs a
**flow** (a data file listing the steps): write the spec, extract requirements, settle the
configuration (the `spec` flow), then write the RTL, format it, lint it and smoke-test it (the `rtl` flow).
Testbench, assertions, verification and the final document were removed.

| Doc | Content |
|---|---|
| [pipeline.md](pipeline.md) | The engine: updates not re-runs, resume, who sees what, the spec step, questions, approval, controls |
| [spec-flow.md](spec-flow.md) | The spec flow (the default): spec → parse → config, artifacts, review API, importing |
| [flows.md](flows.md) | Flows as data (YAML / folders / markdown), gate modes, sessions, tools by role (`config/tools.json`), the TUI picker and panels |
| [rtl-flow.md](rtl-flow.md) | The RTL flow: rules → Verible flags, RTL, format, style lint (LLM judge, fix loop), smoke test; skills for the LLM stages |
| [dv-flow.md](dv-flow.md) | The DV flow: spec → parse → config → `tb` (plain SystemVerilog testbench, or a UVM testbench from an existing VIP with the RTL as the DUT: [dut-dv.md](dut-dv.md)) |
| [fpga-flow.md](fpga-flow.md) | The FPGA flow: SDC, Vivado out-of-context synthesis, timing / utilization report (PPA estimation) |
| [project-flow.md](project-flow.md) | The project settings flow: the project's settings, the tools (found or not, modules), the ASIC libraries (add one by scanning a folder) |
| [asic-flow.md](asic-flow.md) | The ASIC flow: port check, SDC, Design Compiler synthesis (netlist, area / timing / power), Formality equivalence check |
| [sanity-flow.md](sanity-flow.md) | The Sanity flow: CDC / RDC analysis (VC Static) on the shared SDC, a violation table, a waiver review (waive / RTL fix / spec / SDC request / ask you), the check of the waivers |
| [project-layout.md](project-layout.md) | Files Q3TUI reads/writes, pipeline state, staleness, entry points |
| [tui.md](tui.md) | Terminal UI: pipeline dashboard, gates, questions, assistant |
| [architecture.md](architecture.md) | Runtime layers: LLM stages, tools by role, HDL layer |
| [config.md](config.md) | `q3tui.yaml` |
| [team.md](team.md) | Team mode: persistent role sessions — init once, forks, compaction |

## The pipeline

The default flow is `spec` ([spec-flow.md](spec-flow.md)); the `rtl` flow continues from its artifacts:

```text
spec ─► parse ─► config                      (flow `spec`)
spec ─► rules ─► parse ─► config ─► csr ─► rtl ─► format ─► style_lint ─► smoke   (flow `rtl`)
```

| Step | Produces | Skipped when you provide |
|---|---|---|
| **spec** | `spec/spec.md`, sectioned by an editable template | a spec document |
| **parse** | requirements (`REQ-NNN`), parameters, module mapping, ambiguity questions (`schemas/structured_spec.json`) | — |
| **config** | locked parameters, constraints checked by code (`schemas/final_config.json`) | — |
| **rtl** (rtl flow) | SystemVerilog per module, lint clean, synthesis report | — |

The built-in `rtl` flow ([rtl-flow.md](rtl-flow.md)) generates the RTL alone and checks it (rules → Verible flags, CSR from the APB-CSR-Generator,
format, style lint, smoke test); `dv` ([dv-flow.md](dv-flow.md)) writes a testbench from the spec alone (never from the RTL); `fpga`, `asic` and `project` are the synthesis and settings flows. The built-in `example` flow (spec → parse) is a minimal folder flow to copy. Users write
their own flows from the registered step kinds ([flows.md](flows.md)).

Who sees what (enforced): the smoke-test writer and the `tb` step read only spec, parse and config, never the RTL; the RTL stages never see `src/tb/`. Details
in [pipeline.md](pipeline.md) and [spec-flow.md](spec-flow.md).

**Start anywhere.** Each step's inputs are plain files in the project. Provide a spec and
the flow starts at parse. `q3tui run` runs whatever is missing or stale, like `make`.

## Principles

1. **Requirements are the backbone.** Every requirement has an ID (`REQ-001`). RTL
   and checks cite the REQs they implement or check. Every requirement is meant to be checked, not "coverage is high".
2. **Independent checks.** The smoke testbench is written from the spec, the requirements and the
   configuration alone, never from the RTL.
3. **The fix loop is the product.** Failures are not just reported. Triage decides whose
   bug it is and sends a scoped fix to the owning step (the rtl flow: format, style lint and smoke test
   send fixes back to `rtl`). The loop ends when every check passes or the iteration budget runs out.
4. **Deterministic first.** Parsing, lint, synthesis, simulating, log parsing and
   traceability are code. LLMs do writing, reasoning and repair, and they always get tool
   output as ground truth.
5. **Humans at the decision points.** Review gates (their mode set per step); spec ambiguities found anywhere come back to a human as questions. They are
   never silently decided.
6. **Everything is a reviewable file.** The spec, artifacts and RTL are ordinary
   Markdown, JSON and SV in the project tree. The user can edit any of them, and
   downstream steps become stale.

## Scope

- New blocks written from scratch, a few clock domains at most.
- Tools by role (lint, synthesis, simulation, run, optionally mutation), configured in
  `config/tools.json`; the built-in preset is Synopsys (VCS, Design Compiler).
- Interfaces: CLI and TUI (Textual) over one pipeline engine.
- Not in scope: formal, UVM, upgrading existing RTL, web UI.

## State

| Milestone | Scope |
|---|---|
| History | M0–M10 built the engine, the TUI, the assistant, the spec step, team mode and two built-in Q3TUI flows (`units`, `steps`: requirements, architecture, Python golden model, cross-check, triage, signoff). Those flows and their steps have been removed |
| **Current** | Flows as data ([flows.md](flows.md)); the **spec flow** ([spec-flow.md](spec-flow.md)) is the default and the `rtl` flow ([rtl-flow.md](rtl-flow.md)) builds on it; `example` is the template for your own |
| Later | Formal, existing-RTL upgrade, UVM, Verdi debug, web UI, MCP/skills |
