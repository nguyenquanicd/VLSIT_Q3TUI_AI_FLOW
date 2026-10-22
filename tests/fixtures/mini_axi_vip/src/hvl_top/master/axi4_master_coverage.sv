class axi4_master_coverage extends uvm_subscriber #(axi4_master_tx);
  `uvm_component_utils(axi4_master_coverage)
  axi4_master_agent_config axi4_master_agent_cfg_h;
  function new(string name, uvm_component parent);
    super.new(name, parent);
  endfunction
  function void write(axi4_master_tx t);
  endfunction
endclass
