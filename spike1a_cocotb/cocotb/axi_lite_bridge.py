"""
axi_lite_bridge.py -- Spike 1A: Cocotb bridge for the toy AXI4-Lite slave DUT.

Second leg of Spike 1A, built after the FIFO leg (fifo_bridge.py) was
validated. Same shared interface contract:

    reset()                -> dict (state)
    step(action)            -> dict (state)
    get_state()             -> dict
    inject_stimulus(action)  -> None

The interesting difference from the FIFO leg: `action` now needs to encode
a full AXI4-Lite transaction rather than a single push/pop value:

    {"op": "write", "addr": <int>, "data": <int>, "strb": <int>}  # strb optional, defaults to all-bytes
    {"op": "read",  "addr": <int>}
    {"op": "noop"}

And `state` grows to include the register file contents (read directly via
the debug_regs_flat export -- see DUT comment for why) plus the last
read/write result:

    {
        "regs": [<int>, <int>, <int>, <int>],   # full register file snapshot
        "last_read_data": <int>,
        "last_write_resp": <int>,                # 0=OKAY, 2=SLVERR
        "last_read_resp": <int>,
    }

This is exactly the kind of action/state complexity growth that OQ-2
(action space parameterization) is asking about -- this leg is a concrete
data point for that question, not just a second toy DUT for its own sake.
"""

import logging
import time

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ReadOnly, with_timeout
from cocotb.result import SimTimeoutError

LOG = logging.getLogger("fafnir.spike1a.axi_lite_bridge")

DATA_WIDTH = 32
NUM_REGS = 4
TIMEOUT_CYCLES = 100   # guards against AXI handshakes that never complete
                        # (e.g. if Fafnir's RL policy later tries something
                        # pathological like withholding bready forever --
                        # exactly what a_bvalid_progress in the RTL is meant
                        # to catch, but the bridge itself shouldn't hang)


class AxiLiteBridge:
    """
    Cocotb-backed implementation of the shared Fafnir bridge interface for
    the AXI4-Lite slave DUT. Structurally mirrors FifoBridge -- same method
    names, same overall shape -- but inject_stimulus() now drives a full
    AXI4-Lite handshake sequence instead of a single-cycle signal drive.
    """

    def __init__(self, dut):
        self.dut = dut
        self._last_read_data = 0
        self._last_write_resp = 0
        self._last_read_resp = 0
        self._step_count = 0
        # See fifo_bridge.py's identical field for rationale.
        self._last_seen_assertion_count = 0

    async def start_clock(self, period_ns=10):
        cocotb.start_soon(Clock(self.dut.aclk, period_ns, units="ns").start())

    async def reset(self):
        self.dut.aresetn.value = 0
        self.dut.awvalid.value = 0
        self.dut.wvalid.value = 0
        self.dut.bready.value = 1     # always ready to accept write responses
        self.dut.arvalid.value = 0
        self.dut.rready.value = 1     # always ready to accept read data

        for _ in range(4):
            await RisingEdge(self.dut.aclk)

        self.dut.aresetn.value = 1
        await RisingEdge(self.dut.aclk)

        self._last_read_data = 0
        self._last_write_resp = 0
        self._last_read_resp = 0
        self._step_count = 0
        self._last_seen_assertion_count = 0

        return await self.get_state()

    async def _do_write(self, addr: int, data: int, strb: int = None):
        if strb is None:
            strb = (1 << (DATA_WIDTH // 8)) - 1  # all bytes

        self.dut.awaddr.value = addr
        self.dut.awvalid.value = 1
        self.dut.wdata.value = data
        self.dut.wstrb.value = strb
        self.dut.wvalid.value = 1

        # Wait for both AWREADY and WREADY -- this minimal slave accepts
        # them on separate cycles (AW first, then W), so we wait for each
        # independently rather than assuming a single-cycle handshake.
        async def wait_awready():
            while self.dut.awready.value == 0:
                await RisingEdge(self.dut.aclk)
            await RisingEdge(self.dut.aclk)
            self.dut.awvalid.value = 0

        async def wait_wready():
            while self.dut.wready.value == 0:
                await RisingEdge(self.dut.aclk)
            await RisingEdge(self.dut.aclk)
            self.dut.wvalid.value = 0

        try:
            await with_timeout(wait_awready(), TIMEOUT_CYCLES, "ns")
            await with_timeout(wait_wready(), TIMEOUT_CYCLES, "ns")
        except SimTimeoutError:
            LOG.warning("AXI write handshake timed out -- treating as SLVERR")
            self._last_write_resp = 2
            return

        # Wait for BVALID, capture response, then the bready=1 we set at
        # reset means it completes same-cycle from the DUT's perspective.
        while self.dut.bvalid.value == 0:
            await RisingEdge(self.dut.aclk)
        await ReadOnly()
        self._last_write_resp = int(self.dut.bresp.value)
        await RisingEdge(self.dut.aclk)

    async def _do_read(self, addr: int):
        self.dut.araddr.value = addr
        self.dut.arvalid.value = 1

        while self.dut.arready.value == 0:
            await RisingEdge(self.dut.aclk)
        await RisingEdge(self.dut.aclk)
        self.dut.arvalid.value = 0

        while self.dut.rvalid.value == 0:
            await RisingEdge(self.dut.aclk)
        await ReadOnly()
        self._last_read_data = int(self.dut.rdata.value)
        self._last_read_resp = int(self.dut.rresp.value)
        await RisingEdge(self.dut.aclk)

    async def inject_stimulus(self, action: dict):
        op = action.get("op", "noop")

        if op == "write":
            await self._do_write(
                action["addr"], action["data"], action.get("strb")
            )
        elif op == "read":
            await self._do_read(action["addr"])
        else:
            # noop: just advance one cycle
            await RisingEdge(self.dut.aclk)

        self._step_count += 1

    async def step(self, action: dict) -> dict:
        await self.inject_stimulus(action)
        return await self.get_state()

    async def get_state(self) -> dict:
        """
        Reads the register file directly via the debug export rather than
        issuing an AXI read transaction -- see the DUT comment for why.
        This is a deliberate design choice worth flagging: it means
        get_state() does NOT cost any simulation cycles, unlike a "real"
        state read would via the AXI protocol. If Fafnir later needs the
        RL agent's state observations to reflect only what's observable
        through the protocol (i.e. no magic visibility), this debug export
        approach will need revisiting (see
        Fafnir_F4_AXI_RealRead_Alternative.py for a sketched alternative).

        assertion_fired / assertion_fail_count: same diffing scheme as
        FifoBridge.get_state(). IMPORTANT CAVEAT carried over from the
        RTL: only the per-cycle-checkable AWREADY/AWVALID assertion
        currently feeds this counter. The two liveness checks
        (a_bvalid_progress, a_rvalid_progress) are NOT wired into it yet
        -- see axi4_lite_slave.sv's assertions block for why. Don't treat
        assertion_fired==False as proof no liveness violation occurred.
        """
        regs_flat = int(self.dut.debug_regs_flat.value)
        regs = [
            (regs_flat >> (i * DATA_WIDTH)) & ((1 << DATA_WIDTH) - 1)
            for i in range(NUM_REGS)
        ]

        raw_count = int(self.dut.assertion_fail_count.value)
        fired = raw_count != self._last_seen_assertion_count
        self._last_seen_assertion_count = raw_count

        return {
            "regs": regs,
            "last_read_data": self._last_read_data,
            "last_write_resp": self._last_write_resp,
            "last_read_resp": self._last_read_resp,
            "assertion_fired": fired,
            "assertion_fail_count": raw_count,
        }


# -----------------------------------------------------------------------------
# Cocotb test entry points
# -----------------------------------------------------------------------------

@cocotb.test()
async def test_axi_bridge_smoke(dut):
    """
    Sanity check: write a known value to each register, read it back,
    confirm round-trip correctness. Mirrors test_bridge_smoke from the
    FIFO leg, scaled up for multi-field transactions.
    """
    bridge = AxiLiteBridge(dut)
    await bridge.start_clock()
    state = await bridge.reset()
    assert all(r == 0 for r in state["regs"]), "registers should be zero after reset"

    LOG.info("Writing distinct values to all 4 registers...")
    for i in range(NUM_REGS):
        addr = i * 4   # word-aligned, DATA_WIDTH/8 = 4 bytes per register
        value = 0xA5A5_0000 + i
        state = await bridge.step({"op": "write", "addr": addr, "data": value})
        assert state["last_write_resp"] == 0, f"reg {i} write got SLVERR"

    LOG.info("Reading back all 4 registers...")
    for i in range(NUM_REGS):
        addr = i * 4
        expected = 0xA5A5_0000 + i
        state = await bridge.step({"op": "read", "addr": addr})
        assert state["last_read_data"] == expected, (
            f"reg {i}: expected {expected:#x}, got {state['last_read_data']:#x}"
        )

    LOG.info("AXI4-Lite bridge smoke test PASSED.")


@cocotb.test()
async def test_axi_bridge_latency(dut):
    """
    Same methodology as the FIFO leg's latency test: random mix of
    write/read/noop actions, measure wall-clock microseconds/step. Because
    each AXI write/read here takes multiple clock cycles (unlike the
    FIFO's single-cycle push/pop), this number is expected to be HIGHER
    than the FIFO leg's -- that's expected and informative, not a
    regression. The comparison that actually matters for OQ-6 is
    Spike 1A's AXI number vs. Spike 1B's AXI number, once Spike 1B grows
    an equivalent leg.
    """
    import random

    bridge = AxiLiteBridge(dut)
    await bridge.start_clock()
    await bridge.reset()

    n_steps = 1000   # fewer than the FIFO leg's 5000, since each AXI
                       # transaction is several cycles -- keeps total sim
                       # time comparable
    random.seed(42)

    t0 = time.perf_counter()
    for _ in range(n_steps):
        op = random.choice(["write", "read", "noop"])
        if op == "write":
            action = {
                "op": "write",
                "addr": random.choice([0, 4, 8, 12]),
                "data": random.randint(0, 2**32 - 1),
            }
        elif op == "read":
            action = {"op": "read", "addr": random.choice([0, 4, 8, 12])}
        else:
            action = {"op": "noop"}
        await bridge.step(action)
    elapsed = time.perf_counter() - t0

    per_step_us = (elapsed / n_steps) * 1e6
    LOG.info(f"Cocotb AXI4-Lite bridge: {n_steps} steps in {elapsed:.4f}s "
              f"({per_step_us:.2f} us/step)")
