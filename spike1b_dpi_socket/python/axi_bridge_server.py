"""
axi_bridge_server.py -- Spike 1B: Python-side socket server for the
AXI4-Lite leg, counterpart to fifo_bridge_server.py.

Implements the same shared interface contract (reset/step/inject_stimulus/
get_state) over a socket protocol, scaled to the richer AXI transaction
shape. See axi_tb_top.sv and fafnir_dpi_bridge_axi.c for the SV/C side;
the wire protocol here MUST match fafnir_dpi_bridge_axi.c exactly.

Action dict shape (mirrors Spike 1A's axi_lite_bridge.py):

    {"op": "write", "addr": <int>, "data": <int>, "strb": <int>}  # strb optional
    {"op": "read",  "addr": <int>}
    {"op": "noop"}

State dict shape:

    {
        "regs": [<int>, <int>, <int>, <int>],
        "last_read_data": <int>,
        "last_write_resp": <int>,
        "last_read_resp": <int>,
    }

Important granularity difference from the FIFO leg, worth restating here
since it affects how step() is timed for the latency comparison: each
call to step() on this bridge drives ONE COMPLETE AXI TRANSACTION
(multiple clock cycles internally), not one clock cycle. This matches
Spike 1A's AXI leg, which made the same choice -- so the two ARE
comparable to each other, just not directly comparable to either leg's
FIFO-leg numbers (which are single-cycle steps).
"""

import logging
import os
import socket
import struct
import time

LOG = logging.getLogger("fafnir.spike1b.axi_bridge_server")
logging.basicConfig(level=logging.INFO)

# SV -> Python: reg0,reg1,reg2,reg3,last_rdata,last_bresp,last_rresp,assertion_fail_count (8 x int32)
STATE_FMT = "<iiiiiiii"
STATE_SIZE = struct.calcsize(STATE_FMT)
assert STATE_SIZE == 32, "state wire size mismatch with fafnir_dpi_bridge_axi.c"

# Python -> SV: reset_req,op,addr,wdata,wstrb,stop_req (6 x int32)
ACTION_FMT = "<iiiiii"
ACTION_SIZE = struct.calcsize(ACTION_FMT)
assert ACTION_SIZE == 24, "action wire size mismatch with fafnir_dpi_bridge_axi.c"

OP_NOOP = 0
OP_WRITE = 1
OP_READ = 2

DEFAULT_SOCKET_PATH = "/tmp/fafnir_spike1b_axi.sock"
ALL_BYTES_STRB = 0xF   # DATA_WIDTH/8 = 4 bytes, all strobed


class AxiSocketBridge:
    """
    Socket-based implementation of the shared Fafnir bridge interface for
    the AXI4-Lite slave. Structurally identical to FifoSocketBridge (same
    handshake discipline: every recv is a state, every send is the action
    producing the next state) but with the richer 32B/24B message shapes.

    assertion_fired / assertion_fail_count CAVEAT (carried from
    axi4_lite_slave.sv): only the per-cycle-checkable AWREADY/AWVALID
    assertion currently feeds the DUT's counter. The two liveness checks
    (a_bvalid_progress, a_rvalid_progress) are NOT wired in yet. Do not
    treat assertion_fired==False as proof no liveness violation occurred.
    """

    def __init__(self, socket_path: str = DEFAULT_SOCKET_PATH):
        self.socket_path = socket_path
        self._listener = None
        self._conn = None
        # See fifo_bridge_server.py's identical field for rationale.
        self._last_seen_assertion_count = 0

    def listen_for_simulator(self, timeout_s: float = 30.0):
        if os.path.exists(self.socket_path):
            os.remove(self.socket_path)

        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(self.socket_path)
        self._listener.listen(1)
        self._listener.settimeout(timeout_s)

        LOG.info(f"Listening on {self.socket_path}, waiting for simulator...")
        self._conn, _ = self._listener.accept()
        LOG.info("Simulator connected.")

    def _recv_state(self) -> dict:
        buf = self._recv_exact(STATE_SIZE)
        reg0, reg1, reg2, reg3, last_rdata, last_bresp, last_rresp, raw_assertion_count = \
            struct.unpack(STATE_FMT, buf)

        fired = raw_assertion_count != self._last_seen_assertion_count
        self._last_seen_assertion_count = raw_assertion_count

        return {
            "regs": [reg0, reg1, reg2, reg3],
            "last_read_data": last_rdata,
            "last_write_resp": last_bresp,
            "last_read_resp": last_rresp,
            "assertion_fired": fired,
            "assertion_fail_count": raw_assertion_count,
        }

    def _send_action(self, reset_req=0, op=OP_NOOP, addr=0, wdata=0,
                      wstrb=0, stop_req=0):
        buf = struct.pack(ACTION_FMT, reset_req, op, addr, wdata, wstrb, stop_req)
        self._send_exact(buf)

    def _recv_exact(self, n: int) -> bytes:
        chunks = []
        got = 0
        while got < n:
            chunk = self._conn.recv(n - got)
            if not chunk:
                raise ConnectionError("simulator closed the socket unexpectedly")
            chunks.append(chunk)
            got += len(chunk)
        return b"".join(chunks)

    def _send_exact(self, buf: bytes):
        self._conn.sendall(buf)

    def reset(self) -> dict:
        """Same handshake pattern as FifoSocketBridge.reset(): receive
        pre-reset state, request reset, receive post-reset state."""
        self._recv_state()
        self._send_action(reset_req=1)
        return self._recv_state()

    def inject_stimulus(self, action: dict):
        """
        See FifoSocketBridge.inject_stimulus() docstring for why this
        method exists mainly for interface-shape symmetry -- step() is
        what RL code should actually call.
        """
        op_map = {"write": OP_WRITE, "read": OP_READ, "noop": OP_NOOP}
        op = op_map.get(action.get("op", "noop"), OP_NOOP)
        addr = action.get("addr", 0)
        wdata = action.get("data", 0) if op == OP_WRITE else 0
        wstrb = action.get("strb", ALL_BYTES_STRB) if op == OP_WRITE else 0
        self._send_action(op=op, addr=addr, wdata=wdata, wstrb=wstrb)

    def step(self, action: dict) -> dict:
        """
        Same handshake discipline as FifoSocketBridge.step(): receive the
        state produced by the prior call, send this action, receive the
        state this action produces. One call here = one full AXI
        transaction on the SV side (see module docstring on granularity).
        """
        self._recv_state()
        self.inject_stimulus(action)
        return self._recv_state()

    def get_state(self) -> dict:
        """
        Same architectural limitation as FifoSocketBridge.get_state() --
        no out-of-band query exists in this protocol. Raises rather than
        faking a result; see fifo_bridge_server.py for the fuller
        explanation, which applies identically here.
        """
        raise NotImplementedError(
            "Socket protocol has no out-of-band state query. "
            "Use the state dict returned by step()/reset() instead."
        )

    def shutdown(self):
        if self._conn:
            self._send_action(stop_req=1)
            self._conn.close()
        if self._listener:
            self._listener.close()
        if os.path.exists(self.socket_path):
            os.remove(self.socket_path)


# -----------------------------------------------------------------------------
# Phase 1 smoke + latency tests, mirroring Spike 1A's AXI leg tests as
# closely as the protocol allows.
# -----------------------------------------------------------------------------

def test_axi_bridge_smoke():
    """
    Write a distinct value to each of the 4 registers, read them all back,
    confirm round-trip correctness. Mirrors Spike 1A's test_axi_bridge_smoke.
    """
    bridge = AxiSocketBridge()
    bridge.listen_for_simulator()

    state = bridge.reset()
    assert all(r == 0 for r in state["regs"]), "registers should be zero after reset"

    LOG.info("Writing distinct values to all 4 registers...")
    for i in range(4):
        addr = i * 4
        value = 0xA5A5_0000 + i
        state = bridge.step({"op": "write", "addr": addr, "data": value})
        assert state["last_write_resp"] == 0, f"reg {i} write got SLVERR"

    LOG.info("Reading back all 4 registers...")
    for i in range(4):
        addr = i * 4
        expected = 0xA5A5_0000 + i
        state = bridge.step({"op": "read", "addr": addr})
        assert state["last_read_data"] == expected, (
            f"reg {i}: expected {expected:#x}, got {state['last_read_data']:#x}"
        )

    bridge.shutdown()
    LOG.info("AXI4-Lite socket bridge smoke test PASSED.")


def test_axi_bridge_latency():
    """
    Same methodology as Spike 1A's AXI latency test: 1000 random
    write/read/noop steps (fewer than the FIFO leg's 5000, since each AXI
    transaction spans multiple cycles). Directly comparable to Spike 1A's
    AXI number once both have been run -- NOT directly comparable to
    either spike's FIFO-leg numbers, due to the per-transaction vs.
    per-cycle granularity difference noted in the module docstring.
    """
    import random

    bridge = AxiSocketBridge()
    bridge.listen_for_simulator()
    bridge.reset()

    n_steps = 1000
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
        bridge.step(action)
    elapsed = time.perf_counter() - t0

    per_step_us = (elapsed / n_steps) * 1e6
    LOG.info(f"Socket AXI4-Lite bridge: {n_steps} steps in {elapsed:.4f}s "
              f"({per_step_us:.2f} us/step)")

    bridge.shutdown()


if __name__ == "__main__":
    import sys

    test_name = sys.argv[1] if len(sys.argv) > 1 else "smoke"
    if test_name == "smoke":
        test_axi_bridge_smoke()
    elif test_name == "latency":
        test_axi_bridge_latency()
    else:
        print(f"Unknown test: {test_name} (expected 'smoke' or 'latency')")
        sys.exit(1)
