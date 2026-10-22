// Test fixture: a small stand-in with the structure of an AXI4 VIP (not a real VIP)
package axi4_globals_pkg;
`ifdef AXI_MODE_slave
  parameter bit MASTER_AGENT_ACTIVE = 1;
`elsif AXI_MODE_master
  parameter bit MASTER_AGENT_ACTIVE = 0;
`endif
  parameter int ADDRESS_WIDTH = 32;
  parameter int DATA_WIDTH = 64;
  parameter int STROBE_WIDTH = (DATA_WIDTH/8);
  parameter int LENGTH = 8;
  typedef enum bit {WRITE = 1'b1, READ = 1'b0} tx_type_e;
  typedef enum bit [1:0] {OUTSTANDING_WRITE = 2'b00, OUTSTANDING_READ = 2'b01, NON_OUTSTANDING_WRITE = 2'b10, NON_OUTSTANDING_READ = 2'b11} transfer_type_e;
endpackage
