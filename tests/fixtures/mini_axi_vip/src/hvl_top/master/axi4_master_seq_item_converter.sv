class axi4_master_seq_item_converter extends uvm_object;
  `uvm_object_utils(axi4_master_seq_item_converter)
  function new(string name = "axi4_master_seq_item_converter");
    super.new(name);
  endfunction
  static function void to_write_class(axi4_master_tx i, axi4_master_tx o);
    foreach(i.wdata[k]) begin
      if(i.wdata[k] != 0) begin
        o.wdata.push_front(i.wdata[k]);
      end
    end
  endfunction
endclass
