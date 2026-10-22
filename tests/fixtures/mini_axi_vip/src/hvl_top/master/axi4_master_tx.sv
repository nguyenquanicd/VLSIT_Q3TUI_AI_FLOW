class axi4_master_tx extends uvm_sequence_item;
  `uvm_object_utils(axi4_master_tx)
  axi4_master_agent_config axi4_master_agent_cfg_h;
  rand bit [3:0] awid;
  rand bit [ADDRESS_WIDTH-1:0] awaddr;
  rand bit [7:0] awlen;
  rand bit [2:0] awsize;
  rand bit [1:0] awburst;
  rand bit [DATA_WIDTH-1:0] wdata [$];
  rand bit [(DATA_WIDTH/8)-1:0] wstrb [$];
  rand bit [3:0] arid;
  rand bit [ADDRESS_WIDTH-1:0] araddr;
  rand bit [7:0] arlen;
  rand bit [2:0] arsize;
  rand bit [1:0] arburst;
  bit [DATA_WIDTH-1:0] rdata [$];
  rand tx_type_e tx_type;
  rand transfer_type_e transfer_type;
  int no_of_wait_states;
  function new(string name = "axi4_master_tx");
    super.new(name);
  endfunction
endclass
