"""dv step `simulate`: run the tests of the testbench against the released RTL (docs/spec/dv-flow.md). `uvm` kind (docs/spec/dut-dv.md):

The smoke test and, with `uvm_vip.semantic.scope: all`, every directed test whose body the agent implemented, run with the tool role `run_uvm` on the simv the
`tb` step built. The status of every run comes from the simulator log only (rules DUT-DV-012, 023): exit code 0, zero UVM_ERROR and UVM_FATAL, a PASS result line
for the test and no DV_TODO announced; a test that ran clean but made no check is INCOMPLETE, never a PASS. `schemas/dv_evidence.json` records every run that
happened, `reports/dv_test_results.md` lists the tests with the ones not run. No LLM stage; a failure is a finding about the DUT or the testbench, never a reason
to change the expected behavior. The options are the `tb` step's.
"""

from __future__ import annotations

from pathlib import Path

from q3tui.flows.spec.lib import artifacts
from q3tui.flows.spec.lib.base import VlsitStep
from q3tui.pipeline.base import StepContext


class SimulateStep(VlsitStep):
    name = "simulate"
    title = "Simulation"
    deps = ("tb",)  # (+ prepare in the dv flow: its skill.md)
    tool_roles = ("run", "run_uvm")  # run: the plain testbench's tests (+TC=<id>); run_uvm: the UVM tests (+UVM_TESTNAME)

    def _plain(self, engine) -> bool:
        from q3tui.flows.dv import uvm

        return uvm.testbench_kind(uvm.tb_options(engine)) == "plain"

    # -- files -------------------------------------------------------------------------------------

    def inputs(self, engine) -> list[Path]:
        from q3tui.flows.dv import uvm

        tb = engine.by_name["tb"]
        if self._plain(engine):
            prep = [artifacts.path(engine.project, "sw_manifest.json")] if "prepare" in engine.by_name else []
            return [*tb.outputs(engine), *prep]  # (the testbench and the plan with its compile result; the test programs prepare made)
        return [artifacts.path(engine.project, "verification_contract.json"), *uvm.tb_files(tb, engine)]  # (the testbench and what it was integrated with)

    def outputs(self, engine) -> list[Path]:
        from q3tui.flows.dv import uvm

        return uvm.simulate_files(self, engine)

    def reset_files(self, engine) -> list[Path]:
        return [*self.outputs(engine), *super().reset_files(engine)]

    def missing_input(self, engine) -> str | None:
        if self._plain(engine):
            from q3tui.flows.dv.plain import simulate as plain_sim

            return plain_sim.missing_input(engine)
        from q3tui.flows.dv import uvm

        problem = uvm.input_problem(uvm.tb_options(engine), engine.project.root)
        if problem:
            return problem
        contract, why = uvm.frozen_inputs(engine.project)
        if contract is None:
            return why
        if not artifacts.path(engine.project, "dut_integration.json").is_file():
            return "the testbench is not integrated with the RTL yet (step tb: schemas/dut_integration.json)"
        return None

    # -- run -------------------------------------------------------------------------------------------

    async def run(self, ctx: StepContext) -> None:
        from q3tui.flows.dv import uvm

        if self._plain(ctx.engine):
            from q3tui.flows.dv.plain import simulate as plain_sim

            return await plain_sim.run(self, ctx)
        await uvm.run_simulate(self, ctx)
