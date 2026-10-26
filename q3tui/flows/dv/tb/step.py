"""dv step `tb`: the testbench, with the RTL as its DUT (docs/spec/dv-flow.md). Its kind is the `testbench` option (Settings → tb, see `uvm.testbench_kind`): `uvm` — a UVM
testbench from a frozen verification contract and an existing VIP (below); `plain` — a plain SystemVerilog testbench from the reviewed test plan, any IP, no VIP
(`flows/dv/plain/tb.py`: tb_top and one file per test case, compiled with the tool role `sim` against the RTL).

The UVM kind (docs/spec/dut-dv.md):

From the frozen verification contract (step `testplan`) and an existing reusable protocol VIP (read only, never generated): the testbench architecture, the VIP
binding, the deterministic scaffold, the agent's implementation of the regions, compiled against an interface shell. None of those stages ever sees the RTL
(deny_dirs). Then, as the last phase and for the first time, the released RTL as the DUT (rule DUT-DV-026): read by code for structure and compilation, checked
against the frozen interface contract (a difference is a conformance finding, never adapted to), the DUT wrapper and file list, and the compile and elaboration
with the RTL (tool role `sim_uvm`). The RTL is an input of the step, so a changed RTL re-runs it. The simulations are the next step's (`simulate`).
Options (`pipeline.step_options.tb.uvm_vip`): the VIP, the standards, the integration inputs and the semantic scope; the other two steps of the flow read them here.
"""

from __future__ import annotations

from pathlib import Path

from q3tui.flows.spec.lib import artifacts
from q3tui.flows.spec.lib.base import VlsitStep
from q3tui.pipeline.base import StepContext

RTL_SUFFIXES = (".sv", ".v", ".svh", ".vh", ".f")


class TbStep(VlsitStep):
    name = "tb"
    title = "Testbench"
    deps = ("testplan",)
    tool_roles = ("sim", "sim_uvm")  # sim: the plain testbench's compile; sim_uvm: the UVM one's (against the interface shell and with the RTL; without it the compile is NOT_RUN)

    # -- the testbench kind: the plain one is the helper in flows/dv/plain/ (a StepDef of its own, the same step name) --------------------

    def _plain(self):
        from q3tui.flows.dv import uvm

        if uvm.testbench_kind(self.options) != "plain":
            return None
        from q3tui.flows.dv.plain.tb import PlainTb

        impl = PlainTb()
        impl.options = self.options
        return impl

    @property
    def checks_version(self) -> int:  # (the engine re-checks a step when Q3TUI's checks on its result changed)
        plain = self._plain()
        return plain.checks_version if plain is not None else 0

    # -- files -------------------------------------------------------------------------------------

    def _rtl_files(self, engine) -> list[Path]:
        from q3tui.flows.dv import uvm

        try:
            opts = uvm.parse_options(self.options)
        except uvm.OptionsError:
            return []
        rtl = uvm.resolve_path(opts.integration.rtl_dir, engine.project.root)
        return sorted(p for p in rtl.rglob("*") if p.is_file() and p.suffix in RTL_SUFFIXES) if rtl.is_dir() else []

    def inputs(self, engine) -> list[Path]:
        if (plain := self._plain()) is not None:
            return plain.inputs(engine)
        p = engine.project
        frozen = [artifacts.path(p, n) for n in ("verification_contract.json", "golden_manifest.json")]
        return [*frozen, *p.spec_documents(), *self._rtl_files(engine)]  # (the spec's own names are read for the conformance check; the RTL is the DUT)

    def outputs(self, engine) -> list[Path]:
        if (plain := self._plain()) is not None:
            return plain.outputs(engine)
        from q3tui.flows.dv import uvm

        return uvm.tb_files(self, engine)

    def reset_files(self, engine) -> list[Path]:
        return [*self.outputs(engine), *super().reset_files(engine)]

    def on_approve(self, engine) -> None:
        if (plain := self._plain()) is not None:
            plain.on_approve(engine)  # (gate 4 is recorded in selected_testplan.json)

    def missing_input(self, engine) -> str | None:
        if (plain := self._plain()) is not None:
            return plain.missing_input(engine)
        from q3tui.flows.dv import uvm

        problem = uvm.input_problem(self.options, engine.project.root)
        if problem:
            return problem
        contract, why = uvm.frozen_inputs(engine.project)
        if contract is None:
            return why
        from q3tui.flows.dv.uvm import scaffold

        why = scaffold.unsupported(contract)
        if why:
            return why
        opts = uvm.parse_options(self.options)
        if not opts.vip:
            from q3tui.flows.dv.uvm import arch

            needing = arch.needs_vip(contract)
            if needing:
                return (f"the interface(s) {', '.join(needing)} are driven by a protocol VIP and none is set: Settings → tb → VIP location (the existing VIP, e.g. the axi4_avip "
                        "checkout), or answer the contract again for a testbench that drives them itself")
        rtl = uvm.resolve_path(opts.integration.rtl_dir, engine.project.root)
        if not rtl.is_dir():
            return f"the RTL is needed as the DUT ({rtl.relative_to(engine.project.root) if rtl.is_relative_to(engine.project.root) else rtl} is not there: run the rtl flow, or set uvm_vip.integration.rtl_dir)"
        return None

    # -- run -------------------------------------------------------------------------------------------

    async def run(self, ctx: StepContext) -> None:
        from q3tui.flows.dv import uvm

        if (plain := self._plain()) is not None:
            return await plain.run(ctx)
        await uvm.run_testbench(self, ctx)
