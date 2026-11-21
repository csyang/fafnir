# =============================================================================
# Makefile.axi -- Spike 1A Cocotb test runner for the AXI4-Lite leg.
#
# Separate from the FIFO leg's Makefile (different TOPLEVEL/MODULE) but
# otherwise identical structure -- same SIM=vcs/questa portability story.
#
# Usage:
#   make -f Makefile.axi SIM=vcs
#   make -f Makefile.axi SIM=questa
# =============================================================================

TOPLEVEL_LANG ?= verilog
SIM ?= vcs

VERILOG_SOURCES = $(shell pwd)/../rtl/axi4_lite_slave.sv

TOPLEVEL = axi4_lite_slave

MODULE = axi_lite_bridge

export PYTHONPATH := $(shell pwd)/../cocotb:$(PYTHONPATH)

EXTRA_ARGS += +define+FAFNIR_ASSERTIONS

ifeq ($(SIM),vcs)
endif

ifeq ($(SIM),questa)
endif

include $(shell cocotb-config --makefiles)/Makefile.sim
