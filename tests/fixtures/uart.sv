module uart (
    input  logic       clk,
    input  logic       rst_n,
    input  logic       start,
    input  logic [7:0] data,
    output logic       tx,
    output logic       busy
);
    logic [9:0] frame;
    logic [3:0] bits_left;

    assign tx = busy ? frame[0] : 1'b1;
    assign busy = bits_left != 0;

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            frame <= '1;
            bits_left <= '0;
        end else if (busy) begin
            frame <= {1'b1, frame[9:1]};
            bits_left <= bits_left - 1'b1;
        end else if (start) begin
            frame <= {1'b1, data, 1'b0};
            bits_left <= 4'd10;
        end
    end
endmodule
