module uart_fail;
  logic clk = 0, rst_n = 0, valid = 0, tx;
  uart_pkg::uart_byte_t data = '0;
  uart_top dut(.clk, .rst_n, .valid, .data, .tx);
  always #1 clk = ~clk;
  initial begin
    $dumpfile("wave.vcd");
    $dumpvars(0, uart_fail);
    #2;
    if (tx !== 1'b0) $fatal(1, "intentional UART expectation failure");
    $display("CHIPFLOW_TEST_PASS");
    $finish;
  end
endmodule
