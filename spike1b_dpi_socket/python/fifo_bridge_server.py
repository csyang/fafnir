"""
fifo_bridge_server.py -- Spike 1B: Python-side Unix domain socket server,
counterpart to Spike 1A's fifo_bridge.py.

This implements the SAME shared interface contract as Spike 1A's
FifoBridge class, so the two spikes are directly comparable:

    reset()                -> dict (state)
    step(action)            -> dict (state)
    get_state()             -> dict
    inject_stimulus(action)  -> None

The key architectural difference from Spike 1A: this Python process is a
SEPARATE OS process from the simulator. The simulator (driven by
fifo_tb_top.sv + fafnir_dpi_bridge.c) is the socket CLIENT; this script is
the socket SERVER and must be started FIRST.

Wire protocol (must match fafnir_dpi_bridge.c exactly):

    SV -> Python (state), 20 bytes: 5x int32 little-endian
        count, full, empty, pop_data, assertion_fail_count

    Python -> SV (action), 16 bytes: 4x int32 little-endian
        reset_req, op, push_data, stop_req
        op: 0=noop, 1=push, 2=pop

NOTE: state (20B) and action (16B) messages are different sizes -- this
changed when assertion_fail_count was added (previously both were 16B).
See fafnir_dpi_bridge.c for the fuller note on why this two-language
struct-sync is a real maintenance cost of the socket architecture.

Usage:
    python3 fifo_bridge_server.py &      # start listener
    # then launch the simulator (it connects as a client)

Or import FifoSocketBridge directly and drive it from a test harness --
see __main__ block below for the Phase 1 smoke + latency tests, written
to mirror Spike 1A's test_bridge_smoke / test_bridge_latency as closely
as possible.
"""

import logging
import os
import socket
import struct
import time

LOG = logging.getLogger("fafnir.spike1b.fifo_bridge_server")
logging.basicConfig(level=logging.INFO)

STATE_FMT = "<iiiii"   # count, full, empty, pop_data, assertion_fail_count
ACTION_FMT = "<iiii"   # reset_req, op, push_data, stop_req
STATE_SIZE = struct.calcsize(STATE_FMT)
ACTION_SIZE = struct.calcsize(ACTION_FMT)
assert STATE_SIZE == 20, "state wire size mismatch with fafnir_dpi_bridge.c"
assert ACTION_SIZE == 16, "action wire size mismatch with fafnir_dpi_bridge.c"

OP_NOOP = 0
OP_PUSH = 1
OP_POP = 2

DEFAULT_SOCKET_PATH = "/tmp/fafnir_spike1b.sock"


class FifoSocketBridge:
    """
    Socket-based implementation of the shared Fafnir bridge interface.

    Unlike Spike 1A's FifoBridge (which drives Cocotb coroutines directly
    in-process), this class is a thin wrapper around send/recv calls to a
    separate simulator process. The interface shape is identical on
    purpose -- any RL code written against one should work against the
    other with zero changes, which is the whole point of defining the
    shared contract before building either spike.
    """

    def __init__(self, socket_path: str = DEFAULT_SOCKET_PATH):
        self.socket_path = socket_path
        self._listener = None
        self._conn = None
        self._last_pop_data = 0
        # Tracks the last-seen assertion_fail_count so a "fired" bool can
        # be derived without the caller having to diff the raw sticky
        # counter themselves. See FifoBridge (Spike 1A) for the identical
        # rationale -- this exists to keep both spikes' state dicts
        # offering the same derived field, not just the same raw field.
        self._last_seen_assertion_count = 0

    def listen_for_simulator(self, timeout_s: float = 30.0):
        """
        Start listening and block until the simulator process connects.
        Must be called before reset()/step() -- the simulator is the
        socket client and won't exist until it's launched separately.
        """
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
        count, full, empty, pop_data, raw_assertion_count = struct.unpack(STATE_FMT, buf)

        fired = raw_assertion_count != self._last_seen_assertion_count
        self._last_seen_assertion_count = raw_assertion_count

        return {
            "count": count,
            "full": bool(full),
            "empty": bool(empty),
            "pop_data": pop_data,
            "assertion_fired": fired,
            "assertion_fail_count": raw_assertion_count,
        }

    def _send_action(self, reset_req=0, op=OP_NOOP, push_data=0, stop_req=0):
        buf = struct.pack(ACTION_FMT, reset_req, op, push_data, stop_req)
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
        """
        Request a DUT reset.

        Handshake (matches fifo_tb_top.sv's forever loop exactly):
          1. Sim sends current (pre-reset) state -> we receive it here.
          2. We send back reset_req=1.
          3. Sim performs do_reset(), then loops back to step 1, sending
             the post-reset state -> we receive that as our return value.
        """
        self._recv_state()                  # pre-reset state (discarded)
        self._send_action(reset_req=1)        # tell sim to reset
        state = self._recv_state()           # post-reset state
        self._last_pop_data = 0
        return state

    def inject_stimulus(self, action: dict):
        """
        Send one action in response to a state already received via
        get_state()-style usage. In practice, RL code should call step()
        instead, which couples the receive and send correctly -- this
        method is provided only to keep the interface shape symmetric
        with Spike 1A's separable inject_stimulus()/get_state() calls.
        """
        op_map = {"push": OP_PUSH, "pop": OP_POP, "noop": OP_NOOP}
        op = op_map.get(action.get("op", "noop"), OP_NOOP)
        push_data = action.get("data", 0) if op == OP_PUSH else 0
        self._send_action(op=op, push_data=push_data)

    def step(self, action: dict) -> dict:
        """
        The call RL training code actually uses. Handshake:
          1. Receive the state resulting from the PREVIOUS step/reset call
             (this is the state the sim sent and has been blocking on).
          2. Send this action.
          3. Sim drives it, advances one clock, and on its next loop
             iteration sends the resulting state -- which becomes the
             return value here.

        Note this means step() consumes one "state" message per call and
        produces the action that determines the NEXT state -- so the
        state returned by step() N is not the result of action N until
        step N+1 is called and reads it. This is a direct consequence of
        the request/response framing and differs from Spike 1A, where
        inject_stimulus() and get_state() can be called separately
        because Cocotb is in-process and doesn't need a network
        round-trip to "ask" for state.
        """
        self._recv_state()                    # state produced by the prior step
        self.inject_stimulus(action)
        new_state = self._recv_state()        # state produced by THIS action
        if action.get("op") == "pop" and not new_state["empty"]:
            self._last_pop_data = new_state["pop_data"]
        return new_state

    def get_state(self) -> dict:
        """
        NOTE: unlike Spike 1A, this protocol has no out-of-band "peek"
        mechanism -- state is only available as part of the step/reset
        request-response cycle. A pure get_state() would require either
        a richer protocol (separate query message type) or caching the
        last-seen state. This asymmetry with Spike 1A is itself a
        finding worth recording in the Phase 1 decision notes: Cocotb's
        in-process model makes "peek without advancing" free; the socket
        model does not.
        """
        raise NotImplementedError(
            "Socket protocol has no out-of-band state query yet. "
            "Use the state dict returned by step()/reset() instead. "
            "See docstring for why this is architecturally different "
            "from Spike 1A."
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
# Phase 1 smoke + latency tests, mirroring Spike 1A's structure as closely
# as the protocol asymmetry above allows.
# -----------------------------------------------------------------------------

def test_bridge_smoke():
    """
    Same intent as Spike 1A's test_bridge_smoke: push 5 values, pop 5
    values, assert FIFO ordering. Requires the simulator to already be
    running and attempting to connect (launch fifo_tb_top.sv compiled
    with VCS or Questa separately -- see ../sim/Makefile).
    """
    bridge = FifoSocketBridge()
    bridge.listen_for_simulator()

    state = bridge.reset()
    assert state["empty"] is True
    assert state["count"] == 0

    LOG.info("Pushing 5 values...")
    for i in range(5):
        state = bridge.step({"op": "push", "data": i})
        assert state["count"] == i + 1, f"step {i}: expected count {i+1}, got {state['count']}"

    LOG.info("Popping 5 values, checking order (FIFO)...")
    for i in range(5):
        state = bridge.step({"op": "pop"})
        assert state["pop_data"] == i, f"expected {i}, got {state['pop_data']}"

    bridge.shutdown()
    LOG.info("Bridge smoke test PASSED.")


def test_bridge_latency():
    """
    Same methodology as Spike 1A's test_bridge_latency: 5000 random
    push/pop/noop steps, report microseconds/step. Directly comparable
    to the Cocotb number once both have been run.
    """
    import random

    bridge = FifoSocketBridge()
    bridge.listen_for_simulator()
    bridge.reset()

    n_steps = 5000
    random.seed(42)

    t0 = time.perf_counter()
    for _ in range(n_steps):
        op = random.choice(["push", "pop", "noop"])
        action = {"op": op, "data": random.randint(0, 255)} if op == "push" else {"op": op}
        bridge.step(action)
    elapsed = time.perf_counter() - t0

    per_step_us = (elapsed / n_steps) * 1e6
    LOG.info(f"Socket bridge: {n_steps} steps in {elapsed:.4f}s "
              f"({per_step_us:.2f} us/step)")

    bridge.shutdown()


if __name__ == "__main__":
    import sys

    test_name = sys.argv[1] if len(sys.argv) > 1 else "smoke"
    if test_name == "smoke":
        test_bridge_smoke()
    elif test_name == "latency":
        test_bridge_latency()
    else:
        print(f"Unknown test: {test_name} (expected 'smoke' or 'latency')")
        sys.exit(1)
