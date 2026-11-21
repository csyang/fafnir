# Fafnir Spike 1B — DPI-C + Socket Bridge (FIFO + AXI4-Lite legs)

This is the **hand-rolled DPI + socket** half of the Phase 1 simulation-bridge
spike, scoped to be directly comparable to Spike 1A (Cocotb).

## Same DUTs, same interface contract, different transport

Both legs reuse the **identical** RTL from Spike 1A unchanged — same
`fifo.sv` and `axi4_lite_slave.sv`, same parameters, same
`FAFNIR_ASSERTIONS` tap points. The only thing that differs is *how*
Python talks to them.

The Python side (`FifoSocketBridge` / `AxiSocketBridge`) implements the
same nominal interface as Spike 1A's bridge classes: `reset()`,
`step(action)`, `inject_stimulus()`, `get_state()`. **One important
asymmetry surfaced while building the FIFO leg, and confirmed again on
the AXI leg:** `get_state()` cannot be a free "peek" the way it is in
Cocotb — see the docstrings for why, and treat this as a Phase 1 finding
in its own right.

## Layout

```
fafnir_spike1b/
├── rtl/
│   ├── fifo.sv                      # IDENTICAL to Spike 1A's copy
│   └── axi4_lite_slave.sv            # IDENTICAL to Spike 1A's copy
├── dpi/
│   ├── fifo_tb_top.sv                 # FIFO leg: SV testbench, calls fafnir_step() each cycle
│   ├── fafnir_dpi_bridge.c            # FIFO leg: DPI-C shim, 16B fixed wire protocol
│   ├── axi_tb_top.sv                  # AXI leg: SV testbench, calls fafnir_axi_step() per TRANSACTION
│   └── fafnir_dpi_bridge_axi.c        # AXI leg: DPI-C shim, 28B state / 24B action wire protocol
├── python/
│   ├── fifo_bridge_server.py          # FIFO leg: FifoSocketBridge + smoke/latency tests
│   └── axi_bridge_server.py           # AXI leg: AxiSocketBridge + smoke/latency tests
├── sim/
│   └── Makefile                       # `make SIM=vcs|questa` (FIFO) or `make axi SIM=vcs|questa` (AXI)
└── README.md                          # this file
```

## The handshake, precisely (FIFO leg)

Every clock cycle, the SV main loop (`fifo_tb_top.sv`) does, in order:

1. Call `fafnir_step()` — blocks until Python responds
2. Check `stop_req` → `$finish` if set
3. Check `reset_req` → re-run reset and loop back to step 1 if set
4. Otherwise drive `push`/`pop`/`push_data` from the returned action
5. Advance one clock edge
6. Loop back to step 1 (sending the *new* state)

This means from Python's side, every `recv` is a state and every `send` is
the action that produces the *next* state on the following `recv`. Get this
ordering wrong and the bridge will silently desync — this was actually
caught and fixed once already while scoping the FIFO leg, which is itself
a data point on hand-rolled protocol fragility vs. Cocotb's coroutine
model handling this for you.

## The handshake, precisely (AXI4-Lite leg) — and a key granularity difference

The AXI leg's main loop (`axi_tb_top.sv`) follows the same send-state →
recv-action discipline, **but each call to `fafnir_axi_step()` drives one
COMPLETE AXI transaction to completion** (multiple clock cycles internally
via the `do_write`/`do_read` SV tasks), not one clock cycle. This mirrors
the choice Spike 1A's AXI leg made (`AxiLiteBridge.step()` also drives a
full handshake per call), so the two ARE comparable to each other — but
neither AXI leg's latency number is directly comparable to either spike's
FIFO-leg numbers, which step at single-cycle granularity. Keep this in
mind when filling in the comparison table below.

## Running

**FIFO leg** — Terminal 1 (Python listener first):
```bash
cd python && python3 fifo_bridge_server.py smoke      # or: latency
```
Terminal 2 (simulator, connects as client):
```bash
cd sim && make SIM=vcs        # or: make SIM=questa
```

**AXI4-Lite leg** — Terminal 1:
```bash
cd python && python3 axi_bridge_server.py smoke       # or: latency
```
Terminal 2:
```bash
cd sim && make axi SIM=vcs        # or: make axi SIM=questa
```

## Build recipes are NOT yet validated against real tooling

Unlike Spike 1A's Makefiles (which lean entirely on `cocotb-config` to know
the right VCS/Questa invocation), this Makefile's `vcs_build*`/`questa_build*`
targets are best-effort sketches based on standard DPI-C compile/link
conventions for each simulator. They have **not** been run against a real
VCS or Questa install. Expect to need corrections — exact `-CFLAGS`,
whether the `.c` file needs separate compilation via the simulator's own
DPI build step, shared object naming/path conventions, etc. **Log every
correction needed here** — that effort is exactly what this spike is
measuring against Spike 1A's "it just works via `cocotb-config`."

## What's intentionally NOT here yet

- No reward computation, no episode/done logic, no policy — Phase 3/4, same as Spike 1A.
- No results/comparison doc against Spike 1A — Phase 1 "Decision" checklist item; the comparison table below is the template for it.
- `get_state()` raises `NotImplementedError` on both legs — see asymmetry note above. If
  the Phase 1 decision favors this architecture, resolving this gap
  is a follow-up task, not done preemptively here.

## Wire protocol growth — a finding in its own right

Going from the FIFO leg's wire protocol (16 bytes, symmetric in both
directions) to the AXI leg's (28 bytes state / 24 bytes action, asymmetric)
required hand-editing **two struct definitions kept in sync across two
languages** — the C struct in `fafnir_dpi_bridge_axi.c` and the Python
`struct.pack`/`unpack` format string in `axi_bridge_server.py`. A typo or
field-order mismatch between the two would silently corrupt every message.
Spike 1A's equivalent change (growing the action/state dicts for the AXI
leg) required no protocol definition at all — Python dicts don't need a
wire format. This is a concrete, attributable cost of the socket
architecture worth weighing heavily in the Phase 1 decision, not just a
stylistic preference.

## Comparison checklist (fill in once both spikes have been run)

| Metric | Spike 1A (Cocotb) | Spike 1B (DPI+socket) |
|---|---|---|
| Per-step latency, FIFO leg (VCS) | _pending_ | _pending_ |
| Per-step latency, FIFO leg (Questa) | _pending_ | _pending_ |
| Per-step latency, AXI leg (VCS) | _pending_ | _pending_ |
| Per-step latency, AXI leg (Questa) | _pending_ | _pending_ |
| Lines of bridge code, FIFO leg (excl. RTL) | ~150 (Python only) | ~280 (SV + C + Python) |
| Lines of bridge code, AXI leg (excl. RTL) | ~210 (Python only) | ~420 (SV + C + Python) |
| Wire protocol definitions to maintain | 0 | 2 (FIFO: 16B both ways; AXI: 28B/24B asymmetric) |
| Simulator portability mechanism | `cocotb-config` (automatic) | hand-written per-sim Makefile targets |
| `get_state()` as free peek | Yes | No — architectural limitation, see above |
| Action/state growth from FIFO→AXI leg | dict fields only, no contract change | new struct + new format string, kept in sync by hand |
| Build recipe validated against real tooling | Not yet (pending your run) | Not yet (pending your run) |
