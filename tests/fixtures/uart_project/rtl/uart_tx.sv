`include "uart_width.svh"
module uart_tx (
    input logic clk, rst_n, valid,
    input logic [`UART_WIDTH-1:0] data,
    output logic tx
);
  always_ff @(posedge clk) begin
    if (!rst_n)
      tx <= 1'(`UART_RESET);
    else if (valid)
      tx <= ^data;
  end
endmodule
