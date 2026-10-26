"""dv step `testplan`: the tests of the testbench, reviewed before it is built (docs/spec/dv-flow.md). What it makes depends on the testbench kind (`tb` option
`testbench`, see `uvm.testbench_kind`): `uvm` — the verification contract of the UVM testbench (below); `plain` — the test plan of the plain SystemVerilog testbench
(`flows/dv/plain/planning.py`: schemas/test_plan.json, each test case with the REQs it checks, a blocking question for a requirement no test covers).

The UVM kind (docs/spec/dut-dv.md):

Code drafts the verification contract from the approved spec, requirements and configuration (`structured_spec.json`, `final_config.json`, the spec documents,
the approved standards); the `tb_intent` stage completes its semantic columns (objectives, scoreboard rules, coverage goals, the tests with the requirements each
checks); code validates it and freezes it by hash. What the specification leaves open stays an open issue: a question for you (gate: human), with the review
report that says who decided each one. Unspecified behavior is never invented. The step never reads the RTL (rules DUT-DV-001..004, 026): the stage runs with
deny_dirs on it. The options are the `tb` step's (`uvm_vip`: the standards and the VIP shape the contract).
"""

from __future__ import annotations

from pathlib import Path

from q3tui.flows.spec.lib import artifacts
from q3tui.flows.spec.lib.base import VlsitStep
from q3tui.pipeline.base import StepContext


class PlanStep(VlsitStep):
    name = "testplan"
    title = "Test plan"
    deps = ("config",)
    artifact = "verification_contract.json"

    # -- the testbench kind: the plain one is the helper in flows/dv/plain/ (a StepDef of its own, the same step name) --------------------

    def _plain(self, engine):
        from q3tui.flows.dv import uvm

        if uvm.testbench_kind(uvm.tb_options(engine)) != "plain":
            return None
        from q3tui.flows.dv.plain.planning import PlainPlan

        impl = PlainPlan()
        impl.options = self.options
        return impl

    # -- files -------------------------------------------------------------------------------------

    def _standards(self, engine) -> list[Path]:
        """The approved standard documents of the options (behavioral inputs: a change makes the contract stale)."""
        from q3tui.flows.dv import uvm

        try:
            o = uvm.parse_options(uvm.tb_options(engine))
        except uvm.OptionsError:
            return []
        return [p for p in (uvm.resolve_path(s, engine.project.root) for s in o.standards) if p.is_file()]

    def inputs(self, engine) -> list[Path]:
        if (plain := self._plain(engine)) is not None:
            return plain.inputs(engine)
        p = engine.project
        return [*p.spec_documents(), artifacts.path(p, "structured_spec.json"), artifacts.path(p, "final_config.json"), *self._standards(engine),
                self._q_paths(engine)[1]]  # (not the RTL: the contract is built without it)

    def outputs(self, engine) -> list[Path]:
        if (plain := self._plain(engine)) is not None:
            return plain.outputs(engine)
        from q3tui.flows.dv import uvm

        return [*uvm.testplan_files(self, engine), *(f for f in (self._q_paths(engine)[2],) if f.is_file())]

    def reset_files(self, engine) -> list[Path]:
        return [*self.outputs(engine), *super().reset_files(engine)]

    def on_approve(self, engine) -> None:
        if (plain := self._plain(engine)) is not None:
            return plain.on_approve(engine)
        super().on_approve(engine)
        from q3tui.flows.dv import uvm

        uvm.refresh_review_from_disk(self, engine)  # (defaults accepted by this approval show in the review report)

    def missing_input(self, engine) -> str | None:
        if (plain := self._plain(engine)) is not None:
            return plain.missing_input(engine)
        p = engine.project
        if not artifacts.path(p, "structured_spec.json").is_file():
            return "the parsed spec is needed first (step parse: schemas/structured_spec.json)"
        if not artifacts.path(p, "final_config.json").is_file():
            return "the configuration is needed first (step config: schemas/final_config.json)"
        from q3tui.flows.dv import uvm

        return uvm.input_problem(uvm.tb_options(engine), p.root)

    # -- run -------------------------------------------------------------------------------------------

    async def run(self, ctx: StepContext) -> None:
        from q3tui.flows.dv import uvm

        if (plain := self._plain(ctx.engine)) is not None:
            return await plain.run(ctx)
        await uvm.run_testplan(self, ctx)
