# =============================================================================
# Makefile -- Spike 1B DPI+socket bridge, simulator-portable by hand.
#
# Usage (FIFO leg):
#   make SIM=vcs
#   make SIM=questa
#
# Usage (AXI4-Lite leg):
#   make axi SIM=vcs
#   make axi SIM=questa
#
# Unlike Spike 1A (where Cocotb handles simulator portability for us), here
# WE are responsible for getting VCS and Questa to compile/link the same
# DPI-C shim correctly. That hand-maintained portability is exactly the
# extra effort this spike is measuring against Cocotb's built-in version.
#
# Before running: start the matching Python listener FIRST in a separate
# terminal:
#   cd ../python && python3 fifo_bridge_server.py smoke      # FIFO leg
#   cd ../python && python3 axi_bridge_server.py smoke       # AXI leg
# (or `latency` for the latency test) -- the simulator connects to it as a
# socket client, so the listener must be up before `make` launches the sim.
# =============================================================================

SIM ?= vcs

RTL_SRC       = ../rtl/fifo.sv ../dpi/fifo_tb_top.sv
DPI_C_SRC     = ../dpi/fafnir_dpi_bridge.c
TOP           = fifo_tb_top

AXI_RTL_SRC   = ../rtl/axi4_lite_slave.sv ../dpi/axi_tb_top.sv
AXI_DPI_C_SRC = ../dpi/fafnir_dpi_bridge_axi.c
AXI_TOP       = axi_tb_top

.PHONY: all axi clean vcs_build vcs_run questa_build questa_run \
        vcs_build_axi vcs_run_axi questa_build_axi questa_run_axi

all:
ifeq ($(SIM),vcs)
	$(MAKE) vcs_build vcs_run
else ifeq ($(SIM),questa)
	$(MAKE) questa_build questa_run
else
	$(error Unknown SIM=$(SIM), expected 'vcs' or 'questa')
endif

axi:
ifeq ($(SIM),vcs)
	$(MAKE) vcs_build_axi vcs_run_axi
else ifeq ($(SIM),questa)
	$(MAKE) questa_build_axi questa_run_axi
else
	$(error Unknown SIM=$(SIM), expected 'vcs' or 'questa')
endif

# --- VCS — FIFO leg ------------------------------------------------------
# NOTE: exact DPI-C linking flags have NOT been validated against a real
# VCS install yet. Best-effort starting point -- expect to iterate.
vcs_build:
	vcs -full64 -sverilog -debug_access+all \
		-CFLAGS "-I$(VCS_HOME)/include" \
		$(RTL_SRC) $(DPI_C_SRC) \
		-o simv

vcs_run:
	./simv

# --- VCS — AXI4-Lite leg ----------------------------------------------------
# Same caveat as above, scaled to the AXI sources/DPI-C file.
vcs_build_axi:
	vcs -full64 -sverilog -debug_access+all \
		-CFLAGS "-I$(VCS_HOME)/include" \
		$(AXI_RTL_SRC) $(AXI_DPI_C_SRC) \
		-o simv_axi

vcs_run_axi:
	./simv_axi

# --- QuestaSim — FIFO leg --------------------------------------------------
# Similarly unvalidated -- Questa's DPI-C flow typically wants the C
# compiled into a shared object referenced via -sv_lib at vsim time.
questa_build:
	vlog -sv $(RTL_SRC)
	vlib work 2>/dev/null || true
	gcc -shared -fPIC -o fafnir_dpi.so $(DPI_C_SRC) \
		-I$(QUESTA_HOME)/include

questa_run:
	vsim -c -sv_lib fafnir_dpi -do "run -all; quit" $(TOP)

# --- QuestaSim — AXI4-Lite leg ---------------------------------------------
questa_build_axi:
	vlog -sv $(AXI_RTL_SRC)
	vlib work 2>/dev/null || true
	gcc -shared -fPIC -o fafnir_dpi_axi.so $(AXI_DPI_C_SRC) \
		-I$(QUESTA_HOME)/include

questa_run_axi:
	vsim -c -sv_lib fafnir_dpi_axi -do "run -all; quit" $(AXI_TOP)

clean:
	rm -rf simv simv.daidir simv_axi simv_axi.daidir csrc *.so work transcript vsim.wlf
