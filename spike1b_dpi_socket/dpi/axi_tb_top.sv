// =============================================================================
// axi_tb_top.sv -- Spike 1B: DPI-C testbench wrapper for the toy AXI4-Lite slave.
//
// Counterpart to fifo_tb_top.sv, scaled up for AXI4-Lite's richer
// transaction shape. Where the FIFO leg's fafnir_step() took a single
// push_data int, this leg's DPI signature carries addr/data/strb fields
// for writes and an addr field for reads -- directly testing whether the
// hand-rolled DPI+socket approach scales to multi-field transactions as
// cleanly as Spike 1A's dict-based action/state did.
//
// Each call to fafnir_axi_step() drives ONE COMPLETE AXI4-Lite transaction
// to completion (not one clock cycle, unlike the FIFO leg) before
// returning control to Python. This is a deliberate simplification: an
// AXI write/read is itself a multi-cycle handshake, and modeling it as
// "one RL step = one full transaction" avoids needing the C/SV side to
// expose partial-handshake state mid-transaction over the socket. Whether
// this granularity (txn-level vs. cycle-level stepping) matters for the
// RL formulation is a question for Phase 4, not this plumbing spike --
// but the difference itself is a finding, noted in the README.
// =============================================================================

`timescale 1ns/1ps

module axi_tb_top;

    // -- DPI import: implemented in fafnir_dpi_bridge_axi.c -------------------
    // Drives one full AXI transaction (or noop/reset) per call.
    //
    //   reset_req    : 1 if Python wants a DUT reset before this action
    //   op           : 0=noop, 1=write, 2=read
    //   addr         : target register address (byte address)
    //   wdata        : write data (ignored unless op==1)
    //   wstrb        : write byte strobe (ignored unless op==1)
    //   stop_req     : 1 if Python has signaled end-of-episode/shutdown
    //
    //   reg0..reg3   : full register file snapshot, sent back every call
    //                  (mirrors Spike 1A's debug_regs_flat shortcut --
    //                  same zero-extra-cost-state-read compromise, same
    //                  flag-for-revisit-before-Phase-4 caveat applies here)
    //   last_rdata   : data returned by the most recent read (0 if op!=read)
    //   last_bresp   : write response from the most recent write
    //   last_rresp   : read response from the most recent read
    //   assertion_fail_count : sticky DUT counter, added when the
    //     assertion-visibility gap was identified during Phase 3 reward-
    //     aggregator scoping. CAVEAT carried from axi4_lite_slave.sv: only
    //     the per-cycle-checkable AWREADY/AWVALID assertion currently
    //     feeds this counter -- the two liveness checks are not wired in.
    import "DPI-C" function void fafnir_axi_step(
        input  int  reg0, input  int  reg1, input  int  reg2, input  int  reg3,
        input  int  last_rdata, input  int  last_bresp, input  int  last_rresp,
        input  int  assertion_fail_count,
        output bit  reset_req,
        output int  op,
        output int  addr,
        output int  wdata,
        output int  wstrb,
        output bit  stop_req
    );

    // -- DUT hookup ------------------------------------------------------------
    logic aclk, aresetn;
    logic [3:0]  awaddr;
    logic        awvalid, awready;
    logic [31:0] wdata_sig;
    logic [3:0]  wstrb_sig;
    logic        wvalid, wready;
    logic [1:0]  bresp;
    logic        bvalid, bready;
    logic [3:0]  araddr;
    logic        arvalid, arready;
    logic [31:0] rdata_sig;
    logic [1:0]  rresp;
    logic        rvalid, rready;
    logic [127:0] debug_regs_flat;   // DATA_WIDTH(32) * NUM_REGS(4)
    logic [7:0]  assertion_fail_count;

    axi4_lite_slave #(
        .ADDR_WIDTH(4),
        .DATA_WIDTH(32)
    ) dut (
        .aclk(aclk), .aresetn(aresetn),
        .awaddr(awaddr), .awvalid(awvalid), .awready(awready),
        .wdata(wdata_sig), .wstrb(wstrb_sig), .wvalid(wvalid), .wready(wready),
        .bresp(bresp), .bvalid(bvalid), .bready(bready),
        .araddr(araddr), .arvalid(arvalid), .arready(arready),
        .rdata(rdata_sig), .rresp(rresp), .rvalid(rvalid), .rready(rready),
        .debug_regs_flat(debug_regs_flat),
        .assertion_fail_count(assertion_fail_count)
    );

    initial aclk = 0;
    always #5 aclk = ~aclk;

    initial bready = 1;   // always ready to accept write responses
    initial rready = 1;   // always ready to accept read data

    task automatic do_reset();
        aresetn = 0;
        awvalid = 0; wvalid = 0; arvalid = 0;
        repeat (4) @(posedge aclk);
        aresetn = 1;
        @(posedge aclk);
    endtask

    // -- Transaction tasks -----------------------------------------------------
    // Each blocks until the full AXI handshake completes, then returns the
    // captured response -- this is what lets fafnir_axi_step() be a single
    // call per full transaction rather than per clock cycle.

    task automatic do_write(input [3:0] addr_in, input [31:0] data_in,
                              input [3:0] strb_in, output [1:0] resp_out);
        awaddr  = addr_in;
        awvalid = 1;
        wdata_sig = data_in;
        wstrb_sig = strb_in;
        wvalid  = 1;

        wait (awready);
        @(posedge aclk);
        awvalid = 0;

        wait (wready);
        @(posedge aclk);
        wvalid = 0;

        wait (bvalid);
        resp_out = bresp;
        @(posedge aclk);
    endtask

    task automatic do_read(input [3:0] addr_in, output [31:0] data_out,
                             output [1:0] resp_out);
        araddr  = addr_in;
        arvalid = 1;

        wait (arready);
        @(posedge aclk);
        arvalid = 0;

        wait (rvalid);
        data_out = rdata_sig;
        resp_out = rresp;
        @(posedge aclk);
    endtask

    // -- Main drive loop ---------------------------------------------------
    bit  reset_req, stop_req;
    int  op, c_addr, c_wdata, c_wstrb;
    int  last_rdata_i, last_bresp_i, last_rresp_i;
    logic [31:0] rdata_capture;
    logic [1:0]  resp_capture;

    initial begin
        do_reset();
        last_rdata_i = 0;
        last_bresp_i = 0;
        last_rresp_i = 0;

        forever begin
            fafnir_axi_step(
                int'(debug_regs_flat[31:0]),
                int'(debug_regs_flat[63:32]),
                int'(debug_regs_flat[95:64]),
                int'(debug_regs_flat[127:96]),
                last_rdata_i, last_bresp_i, last_rresp_i,
                int'(assertion_fail_count),
                reset_req, op, c_addr, c_wdata, c_wstrb, stop_req
            );

            if (stop_req) begin
                $display("[fafnir_spike1b_axi] stop_req received, ending simulation.");
                $finish;
            end

            if (reset_req) begin
                do_reset();
                last_rdata_i = 0;
                last_bresp_i = 0;
                last_rresp_i = 0;
                continue;
            end

            case (op)
                1: begin // write
                    do_write(c_addr[3:0], c_wdata, c_wstrb[3:0], resp_capture);
                    last_bresp_i = int'(resp_capture);
                end
                2: begin // read
                    do_read(c_addr[3:0], rdata_capture, resp_capture);
                    last_rdata_i = int'(rdata_capture);
                    last_rresp_i = int'(resp_capture);
                end
                default: @(posedge aclk); // noop
            endcase
        end
    end

endmodule
