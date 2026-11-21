// =============================================================================
// axi4_lite_slave.sv -- Minimal AXI4-Lite slave for Fafnir Spike 1A
// (Cocotb bridge PoC, second DUT leg after the FIFO).
//
// Deliberately minimal: a small register file (4 x 32-bit registers),
// full AXI4-Lite write and read channel handshaking, no burst support
// (AXI4-Lite doesn't have bursts), no protection/security signals beyond
// what's structurally required. This is intentionally just complex enough
// to exercise the bridge's ability to handle multi-field transactions
// (address + data + strobe) instead of the FIFO's single-value push/pop --
// that's the actual point of this second leg.
// =============================================================================

module axi4_lite_slave #(
    parameter int ADDR_WIDTH = 4,    // 16 bytes -> 4 x 32-bit registers
    parameter int DATA_WIDTH = 32
) (
    input  logic                       aclk,
    input  logic                       aresetn,    // active-low async reset

    // -- Write address channel --------------------------------------------
    input  logic [ADDR_WIDTH-1:0]      awaddr,
    input  logic                       awvalid,
    output logic                       awready,

    // -- Write data channel -------------------------------------------------
    input  logic [DATA_WIDTH-1:0]      wdata,
    input  logic [(DATA_WIDTH/8)-1:0]  wstrb,
    input  logic                       wvalid,
    output logic                       wready,

    // -- Write response channel ----------------------------------------------
    output logic [1:0]                 bresp,      // 00=OKAY, 10=SLVERR
    output logic                       bvalid,
    input  logic                       bready,

    // -- Read address channel -------------------------------------------------
    input  logic [ADDR_WIDTH-1:0]      araddr,
    input  logic                       arvalid,
    output logic                       arready,

    // -- Read data channel ----------------------------------------------------
    output logic [DATA_WIDTH-1:0]      rdata,
    output logic [1:0]                 rresp,
    output logic                       rvalid,
    input  logic                       rready,

    // Debug/observability export (not part of the AXI protocol -- this is
    // the Fafnir-specific addition for the state encoder). Flattened for
    // DPI/Cocotb visibility simplicity rather than an unpacked array port.
    //
    // NOTE: this was previously (incorrectly) declared as a stray `output`
    // statement in the module body, after the port list had already
    // closed -- not legal ANSI-style SV. Moved into the port list proper
    // as part of this assertion-visibility update; fixing a real bug
    // found while editing this file, not a style change.
    output logic [DATA_WIDTH*NUM_REGS-1:0] debug_regs_flat,

    // Assertion-fail visibility for Phase 3's reward aggregator (added
    // after the gap was identified during reward-formula scoping -- see
    // fifo.sv for the fuller rationale, identical design here). Sticky,
    // clears only on reset. NOTE: only the per-cycle-checkable assertion
    // below currently feeds this counter -- the two liveness checks
    // (a_bvalid_progress, a_rvalid_progress) are NOT yet wired in; see
    // the comment in the assertions block for why.
    output logic [7:0]                 assertion_fail_count
);

    localparam int NUM_REGS = (1 << ADDR_WIDTH) / (DATA_WIDTH / 8);

    // Register file -- observability for the RL state encoder: every
    // register is readable both via the AXI read channel (the "legal" way)
    // and exposed directly as a debug output below (the "for the bridge's
    // get_state(), no AXI transaction needed" way). Real verification
    // environments usually wouldn't expose internal state this directly,
    // but Phase 2's state encoder needs SOME way to read DUT state without
    // burning simulation cycles on a full AXI read transaction for every
    // single state query.
    logic [DATA_WIDTH-1:0] regs [0:NUM_REGS-1];

    genvar gi;
    generate
        for (gi = 0; gi < NUM_REGS; gi++) begin : g_debug_export
            assign debug_regs_flat[gi*DATA_WIDTH +: DATA_WIDTH] = regs[gi];
        end
    endgenerate

    // -- Write FSM -----------------------------------------------------------
    typedef enum logic [1:0] {W_IDLE, W_DATA, W_RESP} wr_state_t;
    wr_state_t wr_state;

    logic [ADDR_WIDTH-1:0] awaddr_latched;

    always_ff @(posedge aclk or negedge aresetn) begin
        if (!aresetn) begin
            wr_state        <= W_IDLE;
            awready         <= 1'b0;
            wready          <= 1'b0;
            bvalid          <= 1'b0;
            bresp           <= 2'b00;
            awaddr_latched  <= '0;
            for (int i = 0; i < NUM_REGS; i++) regs[i] <= '0;
        end else begin
            case (wr_state)
                W_IDLE: begin
                    bvalid <= 1'b0;
                    if (awvalid) begin
                        awready        <= 1'b1;
                        awaddr_latched <= awaddr;
                        wr_state       <= W_DATA;
                    end
                end

                W_DATA: begin
                    awready <= 1'b0;
                    if (wvalid) begin
                        wready <= 1'b1;
                        // Byte-strobed write into the addressed register.
                        for (int b = 0; b < DATA_WIDTH/8; b++) begin
                            if (wstrb[b]) begin
                                regs[awaddr_latched[ADDR_WIDTH-1:2]][b*8 +: 8]
                                    <= wdata[b*8 +: 8];
                            end
                        end
                        bresp    <= 2'b00; // OKAY -- no error injection in this minimal slave
                        bvalid   <= 1'b1;
                        wr_state <= W_RESP;
                    end
                end

                W_RESP: begin
                    wready <= 1'b0;
                    if (bready) begin
                        bvalid   <= 1'b0;
                        wr_state <= W_IDLE;
                    end
                end

                default: wr_state <= W_IDLE;
            endcase
        end
    end

    // -- Read FSM ------------------------------------------------------------
    typedef enum logic [1:0] {R_IDLE, R_DATA} rd_state_t;
    rd_state_t rd_state;

    always_ff @(posedge aclk or negedge aresetn) begin
        if (!aresetn) begin
            rd_state <= R_IDLE;
            arready  <= 1'b0;
            rvalid   <= 1'b0;
            rdata    <= '0;
            rresp    <= 2'b00;
        end else begin
            case (rd_state)
                R_IDLE: begin
                    rvalid <= 1'b0;
                    if (arvalid) begin
                        arready  <= 1'b1;
                        rdata    <= regs[araddr[ADDR_WIDTH-1:2]];
                        rresp    <= 2'b00; // OKAY
                        rd_state <= R_DATA;
                    end
                end

                R_DATA: begin
                    arready <= 1'b0;
                    rvalid  <= 1'b1;
                    if (rvalid && rready) begin
                        rvalid   <= 1'b0;
                        rd_state <= R_IDLE;
                    end
                end

                default: rd_state <= R_IDLE;
            endcase
        end
    end

`ifdef FAFNIR_ASSERTIONS
    // Minimal SVA tap points, mirroring fifo.sv's approach. Same sticky-
    // counter design as fifo.sv -- see that file for the fuller rationale.
    //
    // CORRECTNESS NOTE (found during the same re-trace pass that caught
    // fifo.sv's tautologies): a_awready_implies_awvalid is ALSO a
    // tautology by construction. Tracing the write FSM (W_IDLE/W_DATA/
    // W_RESP above): awready is only ever set to 1 inside `if (awvalid)`
    // in the W_IDLE case -- there is no FSM path where it asserts
    // spontaneously. So this check, as originally written, could never
    // fail under any stimulus, same class of problem as the FIFO's dead
    // checks. Kept below as a documentation-level invariant; a genuinely
    // triggerable check is added afterward.

    // Tautological by construction given the write FSM's structure --
    // kept as a documentation-level invariant / regression check, NOT
    // expected to ever contribute to assertion_fail_count under this RTL.
    wire a_awready_ok = !awready || $past(awvalid) || awvalid;
    a_awready_implies_awvalid: assert property (@(posedge aclk) disable iff (!aresetn)
        a_awready_ok);

    // BVALID must eventually be followed by BREADY (no permanently stuck
    // write response -- a liveness-style check, useful target for Fafnir
    // to try to violate via pathological bready-withholding stimulus)
    a_bvalid_progress: assert property (@(posedge aclk) disable iff (!aresetn)
        bvalid |-> ##[1:64] !bvalid);

    // Same liveness check on the read response side
    a_rvalid_progress: assert property (@(posedge aclk) disable iff (!aresetn)
        rvalid |-> ##[1:64] !rvalid);

    // NOTE: a_bvalid_progress and a_rvalid_progress are multi-cycle
    // (##[1:64]) properties -- SVA's pass/fail for these isn't known on a
    // single clock edge the way the simple combinational checks above
    // are. They are also NOT tautological -- this DUT's bready/rready are
    // tied high at reset in both spikes' testbenches, but a more
    // adversarial stimulus source COULD withhold them, which is exactly
    // the scenario these two checks exist to catch. They are real,
    // triggerable checks, unlike a_awready_ok above -- they're just not
    // yet wired into the sticky counter (see below) because per-cycle
    // pass/fail isn't directly available for a multi-cycle property
    // without a dedicated tracking FSM, which hasn't been built.

    // NEW, actually-triggerable single-cycle check, replacing reliance on
    // a_awready_ok for the counter: address/data channel handshake
    // mismatch -- AWVALID held high for more cycles than
    // AWREADY_TIMEOUT_LIMIT without being accepted indicates the slave
    // (or, more interestingly for Fafnir, a misbehaving combination of
    // slave state and adversarial stimulus timing) has stalled
    // acceptance. Threshold is a toy-DUT placeholder.
    localparam int AWREADY_TIMEOUT_LIMIT = 16;
    logic [4:0] awvalid_wait_count;
    wire        a_awready_timeout_ok = (awvalid_wait_count <= AWREADY_TIMEOUT_LIMIT);

    always_ff @(posedge aclk or negedge aresetn) begin
        if (!aresetn) begin
            awvalid_wait_count <= '0;
        end else if (awvalid && !awready) begin
            awvalid_wait_count <= awvalid_wait_count + 1'b1;
        end else begin
            awvalid_wait_count <= '0;
        end
    end

    a_awready_timeout: assert property (@(posedge aclk) disable iff (!aresetn)
        a_awready_timeout_ok);

    always_ff @(posedge aclk or negedge aresetn) begin
        if (!aresetn) begin
            assertion_fail_count <= '0;
        end else begin
            if (!a_awready_ok || !a_awready_timeout_ok) begin
                assertion_fail_count <= assertion_fail_count + 1'b1;
            end
        end
    end
`else
    // Tie off when assertions are compiled out, so the port still exists
    // and downstream bridges don't need a separate ifdef-aware code path.
    assign assertion_fail_count = '0;
`endif

endmodule
