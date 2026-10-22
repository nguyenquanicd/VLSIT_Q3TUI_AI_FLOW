package axi4_master_pkg;
  `include "uvm_macros.svh"
  import uvm_pkg::*;
  import axi4_globals_pkg::*;
  `include "axi4_master_agent_config.sv"
  `include "axi4_master_tx.sv"
  `include "axi4_master_seq_item_converter.sv"
  `include "axi4_master_write_sequencer.sv"
  `include "axi4_master_read_sequencer.sv"
  `include "axi4_master_coverage.sv"
  `include "axi4_master_agent.sv"
endpackage
