---
label: "4"
title: Timing and utilization report
deps: [fpga_synth]
gate: human
options:
  judge: auto   # is the result the design? auto: judged (LLM) when code finds removed storage / a tiny result; always; off
  max_rounds: null   # a result that is not the design goes to fpga_port this many times in a run, then it is asked here (empty: pipeline.max_fix_iterations)
---

<!-- JUDGE -->
You judge whether an FPGA synthesis result is believable for the design it was made from. The utilization, the timing and the tool's
messages in the task are ground truth; read the RTL (read only) and the spec to know what the design is.

A result is not the design when the tool removed logic the design needs: a memory the RTL declares shows up as no block RAM or LUTRAM, a
processor or a datapath synthesizes to a handful of LUTs, outputs are driven by constants. Out-of-context synthesis keeps only what reaches
an output of the top: storage never written (a memory whose contents the tool does not know is all zeros: a constant ROM), logic whose
result reaches no output and everything fed only by constants is removed by any tool. Find why it never reached an output. Typical causes:
a memory initialized only by a vendor attribute this tool ignores (Quartus `ram_init_file` under Vivado or Yosys), a write port tied off
at the top, an output left unconnected, a parameter or define that turns a block off, the wrong top, a part or tool other than the one
the spec names.

Say roughly what the design should take (from its memories, datapaths and the spec), whether the result matches, and for each cause:
what was lost, the evidence (a log line, a report row, an RTL line), whose fix it is (rtl: one module; spec: a decision of the spec, for
example how a memory is initialized for this part; constraints: the SDC; synthesis_setup: the flow's options, such as the part, the top
or a define; unsure) and the change that would keep the logic. A small design that is small is plausible: never invent a problem.

Compare the run with what the spec says about the target (the part, the tool, the clock): a run on another part or tool than the spec
names, or an SDC clock other than the spec's, is a cause too (synthesis_setup / constraints), even when it is not why logic was removed.

When the result is not the design, the user decides how to go on: give the `question` and 2–4 `options`, the one you recommend first.
Each label is a complete decision worded as the answer itself, the way the user would say it (for example "Synthesize with Quartus on
the spec's Arria 10 10AXF40GAA, where ram_init_file is read", "Bring the IMEM init write port out to the top for this synthesis only",
"Revise the spec to initialize the memories with a file Vivado reads"), with its impact (what changes, what it costs).
<!-- /JUDGE -->
