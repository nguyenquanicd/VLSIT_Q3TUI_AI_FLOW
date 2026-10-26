---
kind: dv_tb
label: '4'
deps:
- testplan
gate: auto
---
The UVM testbench from the frozen contract and an existing reusable protocol VIP (read only, never generated), with the released RTL as its DUT. First the architecture, the VIP binding, the scaffold and the agent's implementation of the regions, compiled against an interface shell: none of these ever sees the RTL. Then, for the first time, the RTL: code reads it for structure and compilation, checks it against the frozen interface contract (a difference is a conformance finding, never adapted to), writes the DUT wrapper and `filelist_dut.f`, and compiles and elaborates the testbench with it (tool role `sim_uvm`). A changed RTL re-runs this step. The **Regions** tab shows what is still open in the testbench, **Conformance** where the RTL differs from the contract. Options: `pipeline.step_options.tb.uvm_vip` (`vip`, `standards`, `integration`, `semantic`).
