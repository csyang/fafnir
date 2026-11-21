// =============================================================================
// fifo_tb_top.sv -- Spike 1B: DPI-C testbench wrapper for the toy FIFO.
//
// This is the SV-side counterpart to Spike 1A's fifo_bridge.py. Where
// Cocotb drives the DUT directly from Python via coroutines, this
// testbench drives the DUT from SV and calls out to a C function
// (fafnir_step) once per clock cycle. The C function owns a Unix domain
// socket connection to a separate Python process and is responsible for:
//   1. serializing current DUT state and sending it to Python
//   2. blocking until Python sends back the next action
//   3. returning that action to SV via DPI output arguments
//
// This SV/C/socket split is intentionally more moving parts than Cocotb's
// single-process model -- that complexity is exactly what Spike 1B is
// measuring against Spike 1A's simplicity.
// =============================================================================

`timescale 1ns/1ps

module fifo_tb_top;

    // -- DPI import: implemented in fafnir_dpi_bridge.c ----------------------
    // Called once per clock cycle. Sends current state out over the socket,
    // blocks for the next action, returns it via output args.
    //
    //   reset_req   : 1 if Python wants a DUT reset before this action
    //   op          : 0=noop, 1=push, 2=pop
    //   push_data   : data byte to push (ignored unless op==1)
    //   stop_req    : 1 if Python has signaled end-of-episode/shutdown
    //   assertion_fail_count : sticky DUT counter, added when the
    //     assertion-visibility gap was identified during Phase 3 reward-
    //     aggregator scoping. See fifo.sv for the counter's own design
    //     rationale; this is just plumbing it through to the socket.
    import "DPI-C" function void fafnir_step(
        input  int  count,
        input  bit  full,
        input  bit  empty,
        input  int  pop_data,
        input  int  assertion_fail_count,
        output bit  reset_req,
        output int  op,
        output int  push_data,
        output bit  stop_req
    );

    // -- DUT hookup ------------------------------------------------------------
    logic clk;
    logic rst_n;
    logic push, pop;
    logic [7:0] push_data;
    logic full, empty;
    logic [7:0] pop_data;
    logic [4:0] count;   // $clog2(16)+1 = 5 bits for DEPTH=16
    logic [7:0] assertion_fail_count;

    fifo #(
        .DATA_WIDTH(8),
        .DEPTH(16)
    ) dut (
        .clk(clk),
        .rst_n(rst_n),
        .push(push),
        .push_data(push_data),
        .full(full),
        .pop(pop),
        .pop_data(pop_data),
        .empty(empty),
        .count(count),
        .assertion_fail_count(assertion_fail_count)
    );

    // -- Clock generation --------------------------------------------------
    initial clk = 0;
    always #5 clk = ~clk;   // 10ns period, matches Spike 1A's default

    // -- Reset + main drive loop --------------------------------------------
    bit  reset_req;
    int  op;
    int  c_push_data;
    bit  stop_req;
    int  last_pop_data;

    task automatic do_reset();
        rst_n = 0;
        push  = 0;
        pop   = 0;
        push_data = 0;
        repeat (4) @(posedge clk);
        rst_n = 1;
        @(posedge clk);
        last_pop_data = 0;
    endtask

    initial begin
        do_reset();

        forever begin
            // Call into C with current state; C blocks on the socket until
            // Python responds with the next action.
            fafnir_step(
                int'(count), full, empty, last_pop_data, int'(assertion_fail_count),
                reset_req, op, c_push_data, stop_req
            );

            if (stop_req) begin
                $display("[fafnir_spike1b] stop_req received, ending simulation.");
                $finish;
            end

            if (reset_req) begin
                do_reset();
                continue;
            end

            // op: 0=noop, 1=push, 2=pop
            push      = (op == 1);
            pop       = (op == 2);
            push_data = (op == 1) ? c_push_data[7:0] : 8'h00;

            @(posedge clk);

            if (pop && !empty) begin
                last_pop_data = int'(pop_data);
            end
        end
    end

endmodule
