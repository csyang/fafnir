# Fafnir Spike 1A — Cocotb Bridge (FIFO + AXI4-Lite legs)

This is the **Cocotb** half of the Phase 1 simulation-bridge spike. The
parallel **DPI + socket** implementation (Spike 1B) is scoped separately and
should be measured the same way so the two are directly comparable.

## What this proves (Phase 1 exit criteria, scoped to Spike 1A)

1. A Python process can drive and read back a SystemVerilog DUT via Cocotb,
   exposing the shared interface contract:
   `reset()`, `step(action)`, `get_state()`, `inject_stimulus(action)`.
2. The same testbench code runs unmodified against **both VCS and Questa**
   (only the `SIM=` make variable changes) — this directly addresses the
   license-contention constraint.
3. Per-step latency is measured with a methodology that Spike 1B will
   reuse, so OQ-6 can be answered with a real number, not a guess.
4. The interface contract holds up under a SECOND, richer DUT (AXI4-Lite
   slave) with multi-field transactions instead of the FIFO's single-value
   push/pop — a concrete data point for OQ-2 (action space parameterization).

## Layout

```
fafnir_spike1a/
├── rtl/
│   ├── fifo.sv                  # minimal parameterized sync FIFO (leg 1)
│   └── axi4_lite_slave.sv        # minimal AXI4-Lite slave, 4x32-bit regs (leg 2)
├── cocotb/
│   ├── fifo_bridge.py            # FifoBridge class + cocotb.test() entry points
│   └── axi_lite_bridge.py        # AxiLiteBridge class + cocotb.test() entry points
├── sim/
│   ├── Makefile                  # FIFO leg: SIM=vcs or SIM=questa
│   └── Makefile.axi              # AXI4-Lite leg: SIM=vcs or SIM=questa
└── README.md                     # this file
```

## Running

**FIFO leg** (from `sim/`):
```bash
make SIM=vcs        # or: make SIM=questa
```

**AXI4-Lite leg** (from `sim/`):
```bash
make -f Makefile.axi SIM=vcs        # or: make -f Makefile.axi SIM=questa
```

Each leg runs two tests:

- `test_*_smoke` — round-trip correctness check (FIFO: push/pop ordering;
  AXI: write-then-read-back across all 4 registers).
- `test_*_latency` — random-action latency measurement, reported in
  microseconds/step. The AXI leg uses fewer steps (1000 vs. the FIFO's
  5000) since each AXI transaction spans several clock cycles where a FIFO
  push/pop is single-cycle — expect the AXI per-step number to be higher
  by design, not as a regression.

## AXI4-Lite leg — what's different from the FIFO leg

The FIFO leg's action space is a single op + optional byte value. The AXI
leg's action space is `{op, addr, data, strb}` — closer to what a real UVM
sequence item looks like for a protocol DUT. This leg is the first real
test of whether the shared interface contract (`step(action)` taking a
dict) scales to richer transactions without needing a redesign. It did,
without changes to the contract shape — only the contents of the `action`
and `state` dicts grew.

One deliberate shortcut worth flagging: `get_state()` reads the register
file via a debug export (`debug_regs_flat`) rather than issuing an actual
AXI read transaction, so state queries cost zero simulation cycles. This
is fine for Phase 1 plumbing validation but means the RL agent's
observations include information not strictly visible through the
protocol. Whether that's acceptable for later phases (it likely isn't,
once we care about realistic stimulus-engine constraints) is flagged in
the bridge code's docstring and should be revisited before Phase 4.

## What's intentionally NOT here yet

- No reward computation, no episode/done logic, no policy — that's Phase 3/4.
- No assertion-failure stimulus on either leg — only legal transactions are
  exercised during this spike, so the `FAFNIR_ASSERTIONS` tap points
  (including the AXI leg's liveness checks on bvalid/rvalid) are compiled
  in but won't fire yet.
- No results/comparison doc against Spike 1B — that's the Phase 1
  "Decision" checklist item, once both spikes exist.

## Open items to watch for during this spike

- Any VCS-vs-Questa behavioral or setup differences (timing, signal
  visibility quirks, license checkout time) should be logged here as
  they're found — these are exactly the "integration friction" notes the
  work plan asks Spike 1A to capture.
- The AXI leg's `get_state()` debug-export shortcut (see above) is a
  known compromise to revisit, not an oversight.
- If UVM coexistence becomes relevant later (e.g. wrapping this bridge
  into an existing UVM environment instead of a bare Cocotb test), that's
  a separate scoping conversation — out of scope for this minimal spike.
