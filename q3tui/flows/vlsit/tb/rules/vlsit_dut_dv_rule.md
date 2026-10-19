# VLSIT DUT Design Verification Rules for the `uvm_vip` TB backend (v1)

> **Purpose:** Rules for the optional `uvm_vip` backend of the VLSIT `tb` step: a UVM DUT testbench built from the approved
> specification and an existing, reusable protocol VIP. The default backend (`plain_sv`) is not governed by this file.
> **Core principle:** SPECIFICATION / APPROVED STANDARD = GOLDEN BEHAVIOR. RTL = DUT IMPLEMENTATION ONLY.

## 0. How to apply this document (for AI agents)

- **MUST / MUST NOT** = mandatory, no waivers. **MAY** = permitted, only as stated. **SHOULD** = default; a deviation needs a written reason.
- Evidence states are `PASS`, `FAIL`, `NOT_RUN` and `N/A`. A missing tool, log or run is `NOT_RUN`, never `PASS`.
- A conflict with a requirement is reported and asked, never hidden. Do not weaken this file to make a run pass.
- Input classes: **behavioral** inputs define expected behavior; **VIP** inputs describe the reusable verification component;
  **integration** inputs describe how the released RTL is built and bound. Integration inputs never define behavior.

## 1. Golden sources and RTL independence

- **DUT-DV-001** MUST: The approved DUT specification (`structured_spec.json` after Gate 1 and the spec documents it came from) and the explicitly approved standards are the authoritative behavioral sources.
- **DUT-DV-002** MUST NOT: RTL shall not be used to derive expected DUT functional behavior.
- **DUT-DV-003** MUST NOT: RTL shall not define scoreboard expected values, testcase intent, assertion intent, functional coverage goals, protocol behavior, or the legal and illegal scenario definitions.
- **DUT-DV-004** MAY: RTL may be inspected only during the separate DUT integration phase, and only for structural and compilation information: top module, ports, parameters needed for elaboration, source files, include directories, compile order, interface binding, elaboration, conformance checks, and debug after a verification failure.
- **DUT-DV-005** MUST: A mismatch between the approved specification and the released RTL shall be reported as a conformance finding and shall not be silently accommodated. Expected behavior is never changed to make the DUT pass.
- **DUT-DV-006** MUST: Unspecified or ambiguous behavior shall be marked `OPEN` or `BLOCKED` and shall not be guessed. A statement the specification itself marks as an assumption, or a requirement flagged for review, is not golden until the user resolves it.
- **DUT-DV-016** MUST: A requirement that cannot be observed at the DUT boundary (internal structure) is recorded as `NOT_OBSERVABLE` with the reason. It is neither dropped silently nor checked through hierarchical references into the DUT.

## 2. Traceability

- **DUT-DV-007** MUST: Every generated testcase traces to one or more approved requirements or approved verification objectives.
- **DUT-DV-008** MUST: Every scoreboard and reference-model behavior retains a source reference to the specification (requirement ID).
- **DUT-DV-009** MUST: Every functional coverage goal traces to an approved requirement or verification objective.
- **DUT-DV-017** MUST: Verification objective, test, scoreboard rule and coverage IDs are permanent: never renumbered or reused. A removed item keeps its ID with status `withdrawn`.
- **DUT-DV-018** MUST: The golden sources are pinned in `golden_manifest.json` (path, revision, SHA-256, approval state). A changed source makes the contract, the testbench and the evidence derived from it stale.

## 3. Existing VIP

- **DUT-DV-010** MUST: The API and behavior of the existing VIP are learned from its documentation and source, never invented. The result is recorded in `vip_integration.json`.
- **DUT-DV-019** MUST: DUT-specific code reaches the VIP only through the generated adapter layer. Scoreboard, coverage and tests do not reference VIP-internal classes.
- **DUT-DV-020** MUST NOT: The flow regenerates, edits or vendors the VIP. The VIP is pinned by location and revision and is read-only input.
- **DUT-DV-021** MUST: The specification decides the supported feature set. A protocol or VIP feature the DUT specification does not support (for example locked or exclusive accesses) is not exercised. Stimulus respects the master obligations of the specification unless a test is an approved negative test of specified behavior.

## 4. Evidence

- **DUT-DV-011** MUST: Planning and generation artifacts are not simulation evidence.
- **DUT-DV-012** MUST NOT: Compile PASS, simulation PASS, regression PASS or coverage closure shall not be reported unless the relevant command actually ran and succeeded.
- **DUT-DV-022** MUST: Every real compile and run records the exact command, return code, log path, `UVM_ERROR` count, `UVM_FATAL` count, test name, seed and status in `dv_evidence.json`. A count that is not read from the log is `NOT_RUN`.
- **DUT-DV-023** MUST: A test result is `PASS` only when the simulator exited with code 0, the log shows zero `UVM_ERROR` and zero `UVM_FATAL`, the result line is `PASS`, and no unimplemented `DV_TODO` lies on the test path.

## 5. Generation

- **DUT-DV-013** MUST: Generated code distinguishes the deterministic scaffold from agent-authored semantic implementation (file header, and `DV_TODO` markers for every open semantic item).
- **DUT-DV-014** MUST: Regeneration shall not silently overwrite hand-written engineer code. A file whose hash differs from the recorded one is treated as edited and is backed up or left alone.
- **DUT-DV-015** MUST: A deterministic smoke test works before constrained-random or stress verification is considered complete.
- **DUT-DV-024** MUST: An unimplemented semantic item (`DV_TODO`) announces itself at run time and is listed by the validator. A stub never passes silently.
- **DUT-DV-025** MUST: Generated TB code follows the VLSIT file conventions (header with project, author and flow, spaces only, comment width) and uses UVM factory creation (`type_id::create`).

## 6. Integration phase

- **DUT-DV-026** MUST: RTL integration starts only after the verification contract and the TB architecture are frozen (recorded by hash). Integration may add files only under the integration paths and may not modify frozen artifacts.
- **DUT-DV-027** MUST: No LLM stage of the behavioral phase reads the RTL (`deny_dirs`). RTL structure reaches the integration code and the compile diagnostics only.
- **DUT-DV-028** MUST: Testbench files outside the integration layer contain no hierarchical references into the DUT and no identifiers taken from the RTL. DUT visibility is through ports and interfaces.
- **DUT-DV-029** MUST: The files and fields read from the RTL are recorded in `dut_integration.json`.
- **DUT-DV-030** MUST: Port and parameter conformance is checked against the interface contract of the specification. A port rename needs an explicit, approved mapping row; there is no silent adaptation.
- **DUT-DV-031** MUST: Only parameter sets the approved specification and configuration declare legal are built and simulated.
