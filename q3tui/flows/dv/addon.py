"""Add-on of the DV flow (q3tui/addons.py): the options of the UVM testbench in the Settings dialog (its `tb` tab) and for the assistant. They are saved to
`pipeline.step_options.tb` in q3tui.yaml, flat, and are the leaves of the nested `uvm_vip` group (`uvm.FLAT_OPTIONS`); a nested group already in q3tui.yaml
(vip, standards, integration, semantic) still works, and the dialog's value wins. All three steps of the flow read them from the `tb` step."""

from __future__ import annotations


def register(api) -> None:
    api.setting("tb", "testbench", "Testbench kind", "choice",
                "plain: a plain SystemVerilog testbench from the reviewed test plan, any IP, no VIP. uvm: a UVM testbench from a verification contract and an existing VIP "
                "(AXI-style memory-mapped IPs only so far). auto (default): uvm when a VIP location is set, plain otherwise.",
                (("auto", "auto — uvm when a VIP is set, else plain"), ("plain", "plain — plain SystemVerilog, any IP"), ("uvm", "uvm — UVM with an existing VIP")), nullable=True)
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
