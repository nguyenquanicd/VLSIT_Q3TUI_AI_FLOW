# Q3TUI — Design Specification

Q3TUI takes a hardware block from **intent to verified RTL**. A pipeline engine runs a
**flow** (a data file listing the steps): write the spec, extract requirements, settle the
configuration, write the RTL and a testbench independently, add assertions, verify
(simulation, SVA, mutation testing, a requirements traceability matrix) and document.

| Doc | Content |
|---|---|
| [pipeline.md](pipeline.md) | The engine: updates not re-runs, resume, who sees what, the spec step, questions, approval, controls |
| [vlsit-flow.md](vlsit-flow.md) | The VLSIT RTL flow (the default): steps, artifacts, RTM, review API |
| [flows.md](flows.md) | Flows as data (YAML / folders / markdown), gate modes, sessions, tools by role (`tools.json`), the TUI picker and panels |
| [fpga-flow.md](fpga-flow.md) | The FPGA flow: SDC, Vivado out-of-context synthesis, timing / utilization report (PPA estimation) |
| [project-layout.md](project-layout.md) | Files Q3TUI reads/writes, pipeline state, staleness, entry points |
| [tui.md](tui.md) | Terminal UI: pipeline dashboard, gates, questions, assistant |
| [architecture.md](architecture.md) | Runtime layers: LLM stages, tools by role, HDL layer |
| [config.md](config.md) | `q3tui.yaml` |
| [team.md](team.md) | Team mode: persistent role sessions — init once, forks, compaction |

## The pipeline

The default flow is `vlsit` ([vlsit-flow.md](vlsit-flow.md)):

```text
spec ─► parse ─► config ─► rtl ‖ tb ─► sva ─► verify ─► doc
```

| Step | Produces | Skipped when you provide |
|---|---|---|
| **spec** | `spec/spec.md`, sectioned by an editable template | a spec document |
| **parse** | requirements (`REQ-NNN`), parameters, module mapping, ambiguity questions (`schemas/structured_spec.json`) | — |
| **config** | locked parameters, constraints checked by code (`schemas/final_config.json`) | — |
| **rtl** | SystemVerilog per module, lint clean, synthesis report | — |
| **tb** | test plan (TC ↔ REQ) and a plain-SystemVerilog testbench | — |
| **sva** | assertions per module (each with its `// NL:` text), bound to the RTL | — |
| **verify** | per-TC simulation, SVA results, mutation testing, the RTM; a bounded fix loop | — |
| **doc** | `docs/specification.md` (+ PDF) | — |

The built-in `example` flow (spec → parse) is a minimal folder flow to copy. Users write
their own flows from the registered step kinds ([flows.md](flows.md)).

Who sees what (enforced): `tb` reads only spec, parse and config — never the RTL. Details
in [pipeline.md](pipeline.md) and [vlsit-flow.md](vlsit-flow.md).

**Start anywhere.** Each step's inputs are plain files in the project. Provide a spec and
the flow starts at parse. `q3tui run` runs whatever is missing or stale, like `make`.

## Principles

1. **Requirements are the backbone.** Every requirement has an ID (`REQ-001`). RTL,
   assertions and test cases cite the REQs they implement or check. Signoff means *every
   requirement is checked and passing* and signed off by a person, not "coverage is high".
2. **Independent checks.** The testbench is written from the spec, the requirements and the
   configuration alone, never from the RTL; assertions and mutation testing add evidence.
3. **The fix loop is the product.** Failures are not just reported. Triage decides whose
   bug it is (RTL, TB, SVA) and sends a scoped fix to the owning step. The loop ends when
   every test passes or the iteration budget runs out.
4. **Deterministic first.** Parsing, lint, synthesis, simulating, log parsing and
   traceability are code. LLMs do writing, reasoning and repair, and they always get tool
   output as ground truth.
5. **Humans at the decision points.** Review gates (their mode set per step) and
   sign-offs; spec ambiguities found anywhere come back to a human as questions. They are
   never silently decided.
6. **Everything is a reviewable file.** The spec, artifacts, RTL, TB and SVA are ordinary
   Markdown, JSON and SV in the project tree. The user can edit any of them, and
   downstream steps become stale.

## Scope

- New blocks written from scratch, a few clock domains at most.
- Tools by role (lint, synthesis, simulation, run, optionally mutation), configured in
  `tools.json`; the built-in preset is Synopsys (VCS, Design Compiler).
- Interfaces: CLI and TUI (Textual) over one pipeline engine.
- Not in scope: formal, UVM, upgrading existing RTL, web UI.

## State

| Milestone | Scope |
|---|---|
| History | M0–M10 built the engine, the TUI, the assistant, the spec step, team mode and two built-in Q3TUI flows (`units`, `steps`: requirements, architecture, Python golden model, cross-check, triage, signoff). Those flows and their steps have been removed |
| **Current** | Flows as data ([flows.md](flows.md)); the **VLSIT flow** ([vlsit-flow.md](vlsit-flow.md)) is the product's flow and the default; `example` is the template for your own |
| Later | Formal, existing-RTL upgrade, UVM, Verdi debug, web UI, MCP/skills |
