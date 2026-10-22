class axi4_master_agent_config extends uvm_object;
  `uvm_object_utils(axi4_master_agent_config)
  uvm_active_passive_enum is_active = UVM_ACTIVE;
  bit has_coverage;
  function new(string name = "axi4_master_agent_config");
    super.new(name);
  endfunction
  function void master_min_addr_range(int n, bit [ADDRESS_WIDTH-1:0] a);
  endfunction
  function void master_max_addr_range(int n, bit [ADDRESS_WIDTH-1:0] a);
  endfunction
endclass
