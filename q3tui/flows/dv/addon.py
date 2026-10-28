"""Add-on of the DV flow (q3tui/addons.py): the options of the UVM testbench in the Settings dialog (its `tb` tab) and for the assistant. They are saved to
`pipeline.step_options.tb` in q3tui.yaml, flat, and are the leaves of the nested `uvm_vip` group (`uvm.FLAT_OPTIONS`); a nested group already in q3tui.yaml
(vip, standards, integration, semantic) still works, and the dialog's value wins. All three steps of the flow read them from the `tb` step."""

from __future__ import annotations


def register(api) -> None:
    api.setting("tb", "testbench", "Testbench kind", "choice",
                "plain: a plain SystemVerilog testbench from the reviewed test plan, any IP, no VIP. uvm: a UVM testbench from a verification contract and an existing VIP "
                "(AXI-style memory-mapped IPs only so far). auto (default): uvm when a VIP location is set, plain otherwise.",
                (("auto", "auto — uvm when a VIP is set, else plain"), ("plain", "plain — plain SystemVerilog, any IP"), ("uvm", "uvm — UVM with an existing VIP")), nullable=True)
    api.setting("tb", "max_rounds", "Simulation fix rounds", "int", "How many times a failing simulation is judged and the testbench fixed before it stops and asks you "
                "(empty: pipeline.max_fix_iterations).", nullable=True)
    api.setting("tb", "handoff_flow", "Hand RTL fixes to flow", "text", "The flow that owns the RTL: an RTL bug the triage finds becomes a change request for its rtl step (default rtl).")
    api.setting("tb", "vip_path", "VIP location", "text",
                "The existing protocol VIP the testbench is built on (a folder, e.g. the axi4_avip checkout; $VARs are expanded). Empty for an IP with no protocol interface to drive: "
                "the flow never generates a VIP, and `tb` waits only when an interface needs one.", nullable=True)
    api.setting("tb", "vip_kind", "VIP kind", "choice", "The family the probe starts from (it still reads the VIP's source).",
                (("axi4_avip", "axi4_avip — mbits-mirafra/axi4_avip"), ("svt", "svt — Synopsys VIP"), ("custom", "custom")), nullable=True)
    api.setting("tb", "vip_revision", "VIP revision", "text", "git commit / tag / version of the VIP, recorded in vip_integration.json (empty: pinned by the fingerprint of its files).",
                nullable=True)
    api.setting("tb", "vip_env", "VIP environment variable", "text",
                "Name of the variable that holds the VIP location (AXI_VIP_HOME): the generated file lists then use $NAME and stay portable.", nullable=True)
    api.setting("tb", "rtl_dir", "RTL folder (the DUT)", "text", "Where the released RTL is, relative to the project (default src/rtl): read by the last phase of `tb`, never before.",
                nullable=True)
    api.setting("tb", "top", "DUT top module", "text", "Empty: the top module of the specification, confirmed by the integration.", nullable=True)
    api.setting("tb", "smoke_test", "Smoke test", "text", "The UVM test `simulate` runs first (default dut_smoke_test).", nullable=True)
    api.setting("tb", "scope", "Semantic scope", "choice", "smoke: the agent implements the smoke path only; all: also the other scoreboard rules and the directed tests.",
                (("smoke", "smoke — the smoke path only"), ("all", "all — also the scoreboard rules and the directed tests")), nullable=True)

    @api.action("riscv_tools_setup", "Find the RISC-V GCC, Spike and dtc the prepare step builds the test programs with ($RISCV/bin, PATH, ~/.q3tui/tools) and write them "
                "into config/tools.json (roles riscv_gcc, iss, sw_build; a role already there is kept). With install: put what is missing into ~/.q3tui/tools first "
                "(the xPack GCC download; Spike and dtc built from source, about 10 minutes; riscv-tests cloned): `q3tui act riscv_tools_setup install=yes`.",
                {"install": "yes: install what is missing first"}, optional=("install",))
    def riscv_tools_setup(ops, install: str = "") -> str:
        from q3tui.flows.dv.prepare import toolchain
        from q3tui.pipeline.engine import EngineError

        p = ops.project
        tc = toolchain.find(None)
        done = []
        if install.strip().lower() in ("yes", "y", "true", "1", "on") and (not tc.ready or tc.tests is None):
            try:
                done = toolchain.install(tc, log=lambda m: ops._log("prepare", m))
            except RuntimeError as exc:
                raise EngineError(str(exc)) from exc
            tc = toolchain.find(None)
        if not (tc.gcc and tc.spike and tc.dtc):
            raise EngineError("; ".join(tc.problems) + " — `q3tui act riscv_tools_setup install=yes` puts them into " + str(toolchain.home_tools())
                              + ", or add the roles riscv_gcc / iss / sw_build to config/tools.json (project flow → Tools)")
        added = toolchain.write_roles(p.config_dir / "tools.json", toolchain.roles_for(tc))
        return ops._log("prepare", (f"installed: {', '.join(done)}; " if done else "") + f"GCC {tc.gcc}, Spike {tc.spike}, dtc {tc.dtc}"
                        + (f"; config/tools.json: added {', '.join(added)}" if added else "; config/tools.json already had the roles"))
