# =============================================================================
# Makefile -- Spike 1A Cocotb test runner, simulator-portable by design.
#
# Usage:
#   make SIM=vcs
#   make SIM=questa
#
# This portability is the whole point of choosing Cocotb for this spike --
# license contention between VCS and QuestaSim means the bridge can't be
# hard-wired to one simulator's toolchain. If this Makefile needs simulator-
# specific hacks beyond the TOPLEVEL_LANG/EXTRA_ARGS below, that is itself
# a finding worth recording in the Phase 1 decision notes.
# =============================================================================

TOPLEVEL_LANG ?= verilog
SIM ?= vcs

# RTL sources
VERILOG_SOURCES = $(shell pwd)/../rtl/fifo.sv

# Top-level module name (must match `module fifo` in fifo.sv)
TOPLEVEL = fifo

# Python test module (must match the .py file, without extension)
MODULE = fifo_bridge

# Make fifo_bridge.py importable from this directory
export PYTHONPATH := $(shell pwd)/../cocotb:$(PYTHONPATH)

# Enable the SVA assertion tap points defined in fifo.sv. These won't fire
# during the Phase 1 smoke/latency tests (legal stimulus only), but leaving
# this on now means Phase 3's reward aggregator can hook into them without
# a rebuild.
EXTRA_ARGS += +define+FAFNIR_ASSERTIONS

# --- Simulator-specific flags -------------------------------------------
ifeq ($(SIM),vcs)
    # VCS-specific compile/run args, if any turn out to be needed.
    # Left empty deliberately -- filling this in (or not needing to) is
    # part of what the spike is measuring.
endif

ifeq ($(SIM),questa)
    # QuestaSim-specific compile/run args, if any turn out to be needed.
endif

include $(shell cocotb-config --makefiles)/Makefile.sim
