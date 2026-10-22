class axi4_master_read_sequencer extends uvm_sequencer #(axi4_master_tx);
  `uvm_component_utils(axi4_master_read_sequencer)
  function new(string name, uvm_component parent);
    super.new(name, parent);
  endfunction
endclass
