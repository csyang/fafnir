// =============================================================================
// fifo.sv -- Minimal synchronous FIFO for Fafnir Spike 1A (Cocotb bridge PoC)
//
// Deliberately simple: single clock domain, parameterized depth/width,
// standard push/pop interface with full/empty flags. No almost-full/almost-
// empty thresholds, no error injection -- those are exactly the kind of
// embellishments that obscure bridge bugs during a plumbing spike.
// =============================================================================

module fifo #(
    parameter int DATA_WIDTH = 8,
    parameter int DEPTH      = 16          // must be a power of 2
) (
    input  logic                   clk,
    input  logic                   rst_n,    // active-low synchronous reset

    // Push (write) interface
    input  logic                   push,
    input  logic [DATA_WIDTH-1:0]  push_data,
    output logic                   full,

    // Pop (read) interface
    input  logic                   pop,
    output logic [DATA_WIDTH-1:0]  pop_data,
    output logic                   empty,

    // Observability (read-only state exposed for the RL state encoder --
    // Phase 2 will decide which of these actually end up in the feature
    // vector, but exposing them now costs nothing).
    output logic [$clog2(DEPTH):0] count,      // 0..DEPTH, one extra bit to
                                                // represent "full" without
                                                // wraparound ambiguity

    // Assertion-fail visibility for Phase 3's reward aggregator (added
    // after the gap was identified during reward-formula scoping: the
    // FAFNIR_ASSERTIONS tap points existed but were never surfaced to the
    // bridge). Sticky -- once incremented, only clears on reset. A bridge
    // reading this once per step can detect ANY failure that occurred
    // since the last read by comparing against its own last-seen value;
    // it does not need to sample every single clock edge.
    output logic [7:0]             assertion_fail_count
);

    localparam int PTR_WIDTH = $clog2(DEPTH);

    logic [DATA_WIDTH-1:0] mem [0:DEPTH-1];

    logic [PTR_WIDTH-1:0]  wr_ptr, rd_ptr;
    logic [PTR_WIDTH:0]    count_r;   // one extra bit: distinguishes full vs empty

    assign full  = (count_r == DEPTH);
    assign empty = (count_r == 0);
    assign count = count_r;

    // Only allow push/pop when legal -- illegal pushes to a full FIFO or
    // pops from an empty FIFO are silently ignored at the RTL level.
    // Spike 1A's reward/penalty logic (Phase 3) is the right place to
    // penalize the RL agent for issuing illegal actions, not the DUT.
    wire do_push = push && !full;
    wire do_pop  = pop  && !empty;

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            wr_ptr  <= '0;
            rd_ptr  <= '0;
            count_r <= '0;
            pop_data <= '0;
        end else begin
            if (do_push) begin
                mem[wr_ptr] <= push_data;
                wr_ptr      <= wr_ptr + 1'b1;
            end

            if (do_pop) begin
                pop_data <= mem[rd_ptr];
                rd_ptr   <= rd_ptr + 1'b1;
            end

            case ({do_push, do_pop})
                2'b10: count_r <= count_r + 1'b1;   // push only
                2'b01: count_r <= count_r - 1'b1;   // pop only
                default: count_r <= count_r;        // both or neither: net zero
            endcase
        end
    end

`ifdef FAFNIR_ASSERTIONS
    // Minimal SVA tap points. These are the assertion failure events that
    // Phase 3's reward aggregator hooks into -- the whole point of Fafnir
    // is to find stimulus sequences that make assertions like these fire.
    //
    // CORRECTNESS NOTE (found during a dedicated re-trace, not originally
    // caught): the three checks below as first written were TAUTOLOGIES,
    // not real checks. do_push already excludes the full case and
    // do_pop already excludes the empty case (see do_push/do_pop above),
    // so count_r is mathematically bounded to [0, DEPTH] by construction
    // -- a_no_overflow_ok and a_no_underflow_ok could never fail under
    // ANY stimulus, legal or illegal. a_full_empty_excl_ok was also
    // always-true by construction for DEPTH > 0. This means the original
    // assertion_fail_count wiring, while not functionally broken
    // (it would correctly report zero failures, forever), gave Fafnir
    // nothing to actually hunt for on this DUT -- a real gap for a
    // project whose whole premise is finding assertion failures.
    //
    // Fixed by keeping the original three as documentation-level
    // invariants (still useful as sanity checks against a future DUT
    // mutation, e.g. via Lesath once it exists), and adding ONE check
    // that genuinely depends on stimulus legality: illegal action
    // attempts (push-while-full, pop-while-empty) are tracked and
    // asserted to never happen MORE than a configurable threshold within
    // an episode -- letting Fafnir's adversarial policy "win" by driving
    // the DUT into a sustained illegal-stimulus regime is a more
    // meaningful target than the original dead checks. This is a
    // judgment call, not the only valid fix -- flagging it as such.
    //
    // assertion_fail_count is incremented (not just set) on every failure
    // so a bridge polling once per step can still detect multiple
    // failures between reads, rather than only ever seeing "1 = something
    // failed." 8 bits is deliberately small -- this is a toy DUT, and
    // wraparound after 255 failures in a single un-reset episode is an
    // acceptable limitation for plumbing validation, not production use.

    // Tautological by construction given do_push/do_pop's guards -- kept
    // as a documentation-level invariant / regression check, NOT expected
    // to ever contribute to assertion_fail_count under this RTL.
    wire a_no_overflow_ok = (count_r <= DEPTH);
    a_no_overflow: assert property (@(posedge clk) disable iff (!rst_n)
        a_no_overflow_ok);

    // Same caveat as above -- count_r is unsigned and bounded by
    // construction, so this can't fail under this RTL.
    wire a_no_underflow_ok = (count_r >= 0);
    a_no_underflow: assert property (@(posedge clk) disable iff (!rst_n)
        a_no_underflow_ok);

    // Same caveat -- true by construction for DEPTH > 0.
    wire a_full_empty_excl_ok = !(full && empty);
    a_full_empty_exclusive: assert property (@(posedge clk) disable iff (!rst_n)
        a_full_empty_excl_ok);

    // NEW, actually-triggerable check: count consecutive cycles where the
    // stimulus attempted an illegal action (push-while-full or
    // pop-while-empty). Unlike the three checks above, THIS one responds
    // to stimulus content -- a policy that hammers push against a full
    // FIFO will trip it. Threshold is arbitrary (toy-DUT placeholder);
    // tune once real episode lengths are known.
    localparam int ILLEGAL_STREAK_LIMIT = 8;
    logic [3:0] illegal_streak;
    wire        illegal_attempt = (push && full) || (pop && empty);
    wire        a_illegal_streak_ok = (illegal_streak <= ILLEGAL_STREAK_LIMIT);

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            illegal_streak <= '0;
        end else if (illegal_attempt) begin
            illegal_streak <= illegal_streak + 1'b1;
        end else begin
            illegal_streak <= '0;
        end
    end

    a_illegal_streak: assert property (@(posedge clk) disable iff (!rst_n)
        a_illegal_streak_ok);

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            assertion_fail_count <= '0;
        end else begin
            if (!a_no_overflow_ok || !a_no_underflow_ok ||
                !a_full_empty_excl_ok || !a_illegal_streak_ok) begin
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
