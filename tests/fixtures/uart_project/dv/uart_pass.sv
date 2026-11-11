module uart_pass;
  logic clk = 0, rst_n = 0, valid = 0, tx;
  uart_pkg::uart_byte_t data = '0;
  uart_top dut(.clk, .rst_n, .valid, .data, .tx);
  always #1 clk = ~clk;
  initial begin
    $dumpfile("wave.vcd");
    $dumpvars(0, uart_pass);
    #2;
    if (tx !== 1'b1) $fatal(1, "reset must keep UART idle");
    rst_n = 1;
    valid = 1;
    data = 8'h03;
    #2;
    if (tx !== 1'b0) $fatal(1, "unexpected parity");
    $display("CHIPFLOW_TEST_PASS");
    $finish;
  end
endmodule
