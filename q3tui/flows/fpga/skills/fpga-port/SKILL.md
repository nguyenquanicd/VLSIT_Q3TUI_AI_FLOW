---
name: fpga-port
description: Port RTL to an FPGA part and fix what its synthesis tool rejects — memory initialization files, vendor attributes and primitives, the synthesis setup — or hand the change to its owner.
---

# Porting RTL to an FPGA part

The FPGA flow runs `fpga_port` → `sdc` → `fpga_synth` → `timing_util_report`. Every problem of the synthesis comes back to you as a change
request with the tool's own error lines; a result that is not the design (logic removed) comes back with the judgment. You fix what is the
FPGA flow's, hand the RTL's changes to the flow that owns the RTL, and ask the user what only they decide. The synthesis runs again after you.

## Method
1. Read the problem: the scan's findings and the error lines handed to you. Find the line of the RTL each one points at (Read / Grep).
2. Decide whose it is:
   - **The FPGA flow's** (yours): a memory initialization file that is missing, the top module, a define the RTL needs for synthesis, a
     tool setting. Fix it with your tools, then `port_scan`.
   - **The RTL's**: a construct this tool rejects or cannot map, another vendor's primitive, a write port the design needs and does not
     have. An `order` for the module: the change, concretely, and why (the error line). Behaviour, ports and requirement tags stay.
   - **The user's**: a choice the spec does not settle (which program a memory boots with when several fit), the part or the tool (the
     spec names another one, the design does not fit), a spec decision that does not hold here. A `question` with options.
   - **The environment**: the tool is not installed, a licence, a library of the install is missing. Say what to install or set.
3. Do the change once, then check. Do not repeat a change that did not help: say why it did not.

## Memory initialization files
- The RTL names them: Quartus' attribute `(* ram_init_file = "m_imem.mif" *)` on an array, or `$readmemh("x.hex", mem)` / `$readmemb`.
  The flow finds a file beside the RTL or in `src/fpga/` and gives it to the tool. Quartus Pro stops when one is missing (error 20059);
  Quartus Standard, Vivado and Yosys leave the memory empty, and a memory nothing writes then becomes a ROM of zeros that is removed with
  everything that only reads it (the result looks far too small).
- `ram_init_file` is read by Quartus only. Vivado and Yosys ignore it: under them only `$readmemh` (or an `initial` block) initializes a
  memory — an RTL change, and a spec decision when the spec forbids `$readmemh`: then ask (another part with Quartus, or allow it).
- What should be in the memory comes from the spec (the firmware it runs, the weights it holds) and from the images the project has: a
  dv flow builds its programs into `build/tb/programs/<program>.imem.hex` / `.dmem.hex` (`$readmemh` text, one word per line). Pick the
  image the spec says the FPGA boots with; when the spec does not say and more than one program fits, ask (the options name the images).
- `mem_image` converts: give the source image, the target as the RTL names it, and the array's depth (words) and width (bits) from the
  module's declaration (parameters resolved: read them). A `.mif` target becomes a Quartus MIF (the addresses the image leaves out are 0).

## By tool
- **Quartus Prime** (Intel): error lines `Error (<id>): …`; synthesis arguments are assignments (`set_global_assignment -name NAME VALUE`).
  Pro: `quartus_syn`, Standard: `quartus_map`. 20059: an initialization file not found; 10161 / 10170: a Verilog syntax / object the
  tool does not know; 12006: a module not found (the filelist, or a primitive of another vendor); 13024: outputs stuck at a constant.
- **Vivado** (Xilinx 7-series): `ERROR: [Synth 8-…]`; arguments are flags of `synth_design` (`-flatten_hierarchy rebuilt`, …). Synth 8-439:
  a module not found; Synth 8-6014 / 8-3332: storage removed (look why nothing reads or writes it).
- **Yosys + nextpnr** (Lattice): `ERROR: …`; a design that does not fit the device, or needs more IOs than the package has, stops in nextpnr:
  that is the part's question.
