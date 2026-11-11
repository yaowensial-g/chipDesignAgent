module uart_top (
    input logic clk, rst_n, valid,
    input uart_pkg::uart_byte_t data,
    output logic tx
);
  uart_tx transmitter(.clk, .rst_n, .valid, .data, .tx);
endmodule
