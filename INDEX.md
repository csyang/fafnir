# Fafnir Phase 1 — Bridge Testing Bundle

Everything needed to run both Phase 1 bridge spikes, bundled together
since the work plan's Decision step needs results from both.

## Contents

```
fafnir_phase1_bridge_testing/
├── spike1a_cocotb/          -- Cocotb-based bridge (FIFO + AXI4-Lite legs)
│   ├── rtl/
│   │   ├── fifo.sv
│   │   └── axi4_lite_slave.sv
│   ├── cocotb/
│   │   ├── fifo_bridge.py
│   │   └── axi_lite_bridge.py
│   ├── sim/
│   │   ├── Makefile           (FIFO leg: make SIM=vcs|questa)
│   │   └── Makefile.axi       (AXI leg: make -f Makefile.axi SIM=vcs|questa)
│   └── README.md
│
└── spike1b_dpi_socket/      -- Hand-rolled DPI-C + Unix socket bridge
    ├── rtl/
    │   ├── fifo.sv                    (identical to spike1a's copy)
    │   └── axi4_lite_slave.sv          (identical to spike1a's copy)
    ├── dpi/
    │   ├── fifo_tb_top.sv
    │   ├── fafnir_dpi_bridge.c
    │   ├── axi_tb_top.sv
    │   └── fafnir_dpi_bridge_axi.c
    ├── python/
    │   ├── fifo_bridge_server.py
    │   └── axi_bridge_server.py
    ├── sim/
    │   └── Makefile        (FIFO: make SIM=vcs|questa; AXI: make axi SIM=vcs|questa)
    └── README.md
```

16 files total, 8 per spike. Each spike's own README has full run
instructions, handshake details, and known caveats — start there.

## Status of this code, as of this bundle

Both spikes include the corrections from this session's work:

- **Assertion visibility**: both DUTs now expose a sticky
  `assertion_fail_count` output, surfaced into both bridges'
  `get_state()`/`step()` returns as `assertion_fired` (bool) +
  `assertion_fail_count` (raw count).
- **Assertion correctness fix**: the original SVA checks on both DUTs
  were found to be tautologies (could never fail under any stimulus,
  by construction — see each RTL file's inline comments for the full
  trace). Kept as harmless documentation-level invariants and replaced
  with genuinely triggerable checks (an illegal-action streak counter on
  the FIFO, an AWREADY acceptance timeout on the AXI leg).
- A real pre-existing bug (`debug_regs_flat` declared as an invalid
  stray port) was also found and fixed in `axi4_lite_slave.sv`.

**None of this has been run against a real simulator yet.** Syntax was
checked (Python compiles cleanly; C syntax-checked against a stubbed
`svdpi.h`; SV was hand-traced but not elaborated by a real tool), and
wire-protocol struct sizes/field-ordering were verified by hand across
the SV/C/Python boundary in Spike 1B. That's not a substitute for an
actual VCS/Questa run — treat the first real run on each spike as a
genuine correctness test, not a formality.

## Suggested run order

1. Spike 1A FIFO leg (simplest, fastest feedback if something's wrong)
2. Spike 1B FIFO leg (same DUT, different bridge — good comparison point early)
3. Spike 1A AXI leg
4. Spike 1B AXI leg

Each spike's smoke test should pass before moving to its latency test.
Drop results into `Fafnir_F4_Phase1_Results_Template.md` (separate file,
not included in this bundle) as you go.
