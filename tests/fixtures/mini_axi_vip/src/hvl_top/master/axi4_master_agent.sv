class axi4_master_agent extends uvm_agent;
  `uvm_component_utils(axi4_master_agent)
  axi4_master_agent_config axi4_master_agent_cfg_h;
  axi4_master_write_sequencer axi4_master_write_seqr_h;
  axi4_master_read_sequencer axi4_master_read_seqr_h;
  axi4_master_coverage axi4_master_cov_h;
  function new(string name = "axi4_master_agent", uvm_component parent = null);
    super.new(name, parent);
  endfunction
  function void connect_phase(uvm_phase phase);
    if(axi4_master_agent_cfg_h.is_active == UVM_ACTIVE) begin
      axi4_master_cov_h.axi4_master_agent_cfg_h = axi4_master_agent_cfg_h;
    end
  endfunction
endclass
