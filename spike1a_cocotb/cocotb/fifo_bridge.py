"""
fifo_bridge.py -- Spike 1A: Cocotb bridge for the Fafnir toy FIFO DUT.

This implements the SHARED interface contract that both Spike 1A (Cocotb)
and Spike 1B (DPI+socket) must expose, so the two spikes are a fair,
apples-to-apples comparison:

    reset()                -> None
    step(action)            -> dict (state)
    get_state()             -> dict
    inject_stimulus(action)  -> None   (alias used by step() internally)

`action` is a plain Python dict so it's trivially comparable to whatever
Spike 1B serializes over its socket:

    {"op": "push", "data": <int>}
    {"op": "pop"}
    {"op": "noop"}

`state` (the return of get_state()) is also a plain dict:

    {
        "count": <int>,
        "full": <bool>,
        "empty": <bool>,
        "pop_data": <int>,      # last popped value (valid only after a pop)
    }

This file is written as a standalone Cocotb test module. Run via:
    make SIM=vcs       (or SIM=questa)
from the sim/ directory (Makefile provided separately).
"""

import logging
import time

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ReadOnly

LOG = logging.getLogger("fafnir.spike1a.fifo_bridge")


class FifoBridge:
    """
    Cocotb-backed implementation of the shared Fafnir bridge interface.

    Deliberately thin: no RL logic lives here. This class's only job is to
    translate {reset, step, get_state, inject_stimulus} into Cocotb signal
    drives/reads. Reward computation, episode bookkeeping, and the actual
    policy belong in later phases.
    """

    def __init__(self, dut):
        self.dut = dut
        self._last_pop_data = 0
        self._step_count = 0
        # Tracks the last-seen assertion_fail_count value so get_state()
        # can report "did a NEW failure happen since the last read" rather
        # than the DUT's raw sticky counter, which the reward aggregator
        # would otherwise have to diff itself.
        self._last_seen_assertion_count = 0

    async def start_clock(self, period_ns=10):
        cocotb.start_soon(Clock(self.dut.clk, period_ns, units="ns").start())

    async def reset(self):
        """Drive synchronous reset for a few cycles, then release."""
        self.dut.rst_n.value = 0
        self.dut.push.value = 0
        self.dut.pop.value = 0
        self.dut.push_data.value = 0

        for _ in range(4):
            await RisingEdge(self.dut.clk)

        self.dut.rst_n.value = 1
        await RisingEdge(self.dut.clk)

        self._last_pop_data = 0
        self._step_count = 0
        self._last_seen_assertion_count = 0

        return await self.get_state()

    async def inject_stimulus(self, action: dict):
        """
        Drive one action onto the DUT inputs for one clock cycle.
        This is the part that, in Spike 1B, would instead be serialized
        and sent over a Unix domain socket to a separate SV process.
        """
        op = action.get("op", "noop")

        self.dut.push.value = 1 if op == "push" else 0
        self.dut.pop.value = 1 if op == "pop" else 0
        self.dut.push_data.value = action.get("data", 0) if op == "push" else 0

        await RisingEdge(self.dut.clk)

        # Sample outputs after the clock edge settles (avoids race with the
        # always_ff block -- ReadOnly phase is the Cocotb-idiomatic way to
        # do this rather than an ad-hoc delay).
        await ReadOnly()

        if op == "pop" and self.dut.empty.value == 0:
            self._last_pop_data = int(self.dut.pop_data.value)

        self._step_count += 1

    async def step(self, action: dict) -> dict:
        """Combined inject_stimulus + get_state, mirroring a gym-style step()."""
        await self.inject_stimulus(action)
        return await self.get_state()

    async def get_state(self) -> dict:
        """
        Read current DUT state. No clock edge advanced here -- this is a
        pure read, so it can be called mid-cycle for debugging without
        disturbing the simulation.

        assertion_fired / assertion_fail_count: closes the gap identified
        during Phase 3 reward-aggregator scoping, where assertion failures
        were not visible to the bridge despite FAFNIR_ASSERTIONS tap
        points existing in the RTL. `assertion_fail_count` is the DUT's
        raw sticky counter (since last reset); `assertion_fired` is
        derived here by diffing against the last value this bridge saw,
        so the reward aggregator gets a simple bool for "something new
        failed since my last read" without re-deriving the diff itself.
        """
        raw_count = int(self.dut.assertion_fail_count.value)
        fired = raw_count != self._last_seen_assertion_count
        self._last_seen_assertion_count = raw_count

        return {
            "count": int(self.dut.count.value),
            "full": bool(self.dut.full.value),
            "empty": bool(self.dut.empty.value),
            "pop_data": self._last_pop_data,
            "assertion_fired": fired,
            "assertion_fail_count": raw_count,
        }


# -----------------------------------------------------------------------------
# Cocotb test entry points
# -----------------------------------------------------------------------------

@cocotb.test()
async def test_bridge_smoke(dut):
    """
    Sanity check: bridge can reset, push N items, pop them back, and the
    state read-back matches what was pushed. This is NOT an RL episode --
    it's the Phase 1 exit criterion: prove the round-trip works at all
    before anything resembling a policy exists.
    """
    bridge = FifoBridge(dut)
    await bridge.start_clock()
    state = await bridge.reset()
    assert state["empty"] is True
    assert state["count"] == 0

    LOG.info("Pushing 5 values...")
    for i in range(5):
        state = await bridge.step({"op": "push", "data": i})
        assert state["count"] == i + 1

    assert state["full"] is False
    assert state["empty"] is False

    LOG.info("Popping 5 values, checking order (FIFO)...")
    for i in range(5):
        state = await bridge.step({"op": "pop"})
        assert state["pop_data"] == i, f"expected {i}, got {state['pop_data']}"

    assert state["empty"] is True
    LOG.info("Bridge smoke test PASSED.")


@cocotb.test()
async def test_bridge_latency(dut):
    """
    Phase 1 / OQ-6: measure per-step wall-clock latency of the bridge.
    This is a Python-side wall-clock measurement of the inject_stimulus()
    call, averaged over a few thousand steps with a random push/pop mix.
    The same measurement methodology should be used in Spike 1B so the
    two numbers are comparable.
    """
    import random

    bridge = FifoBridge(dut)
    await bridge.start_clock()
    await bridge.reset()

    n_steps = 5000
    random.seed(42)

    t0 = time.perf_counter()
    for _ in range(n_steps):
        op = random.choice(["push", "pop", "noop"])
        action = {"op": op, "data": random.randint(0, 255)} if op == "push" else {"op": op}
        await bridge.step(action)
    elapsed = time.perf_counter() - t0

    per_step_us = (elapsed / n_steps) * 1e6
    LOG.info(f"Cocotb bridge: {n_steps} steps in {elapsed:.4f}s "
              f"({per_step_us:.2f} us/step)")

    # Not a hard pass/fail gate yet -- Phase 1's decision step compares this
    # number against Spike 1B's equivalent measurement. Logged here so it
    # shows up in the regression/CI output without needing a separate
    # results file.
    assert per_step_us < 5000, (
        f"Bridge latency {per_step_us:.2f} us/step is suspiciously high; "
        f"investigate before using this number in the Phase 1 comparison."
    )
