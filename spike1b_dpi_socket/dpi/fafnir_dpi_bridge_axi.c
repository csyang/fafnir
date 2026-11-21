/*
 * fafnir_dpi_bridge_axi.c -- Spike 1B: DPI-C <-> socket bridge, AXI4-Lite leg.
 *
 * Counterpart to fafnir_dpi_bridge.c (FIFO leg), scaled to a richer wire
 * protocol carrying multi-field transactions. This is the AXI-leg
 * equivalent of Spike 1A's axi_lite_bridge.py action/state dict growth --
 * here, growing the wire protocol means hand-editing two struct
 * definitions (this file AND the Python side) and keeping them in sync
 * manually, which is exactly the maintenance cost Spike 1A's dict-based
 * approach doesn't pay.
 *
 * Wire protocol (must match axi_bridge_server.py exactly):
 *
 *   SV -> Python (state message), 32 bytes:
 *     int32  reg0, reg1, reg2, reg3   (register file snapshot)
 *     int32  last_rdata
 *     int32  last_bresp
 *     int32  last_rresp
 *     int32  assertion_fail_count   (added when the assertion-visibility
 *                                    gap was identified during Phase 3
 *                                    reward-aggregator scoping)
 *
 *   Python -> SV (action message), 24 bytes:
 *     int32  reset_req   (0 or 1)
 *     int32  op          (0=noop, 1=write, 2=read)
 *     int32  addr
 *     int32  wdata
 *     int32  wstrb
 *     int32  stop_req    (0 or 1)
 *
 * Unlike the FIFO leg's single fixed size for both directions, this
 * protocol has DIFFERENT sizes for state (32B) vs. action (24B) --
 * worth flagging as another small but real source of hand-rolled-protocol
 * bug surface that a framework like Cocotb simply doesn't have, since it
 * has no wire protocol at all.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <errno.h>
#include <stdint.h>

#include "svdpi.h"

#define DEFAULT_SOCKET_PATH "/tmp/fafnir_spike1b_axi.sock"

typedef struct {
    int32_t reg0, reg1, reg2, reg3;
    int32_t last_rdata;
    int32_t last_bresp;
    int32_t last_rresp;
    int32_t assertion_fail_count;
} axi_state_msg_t;   /* 32 bytes */

typedef struct {
    int32_t reset_req;
    int32_t op;
    int32_t addr;
    int32_t wdata;
    int32_t wstrb;
    int32_t stop_req;
} axi_action_msg_t;  /* 24 bytes */

static int g_sock_fd = -1;
static int g_connected = 0;

static void fafnir_axi_connect(void) {
    const char *path = getenv("FAFNIR_AXI_SOCKET_PATH");
    if (path == NULL) path = DEFAULT_SOCKET_PATH;

    g_sock_fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (g_sock_fd < 0) {
        perror("[fafnir_dpi_bridge_axi] socket() failed");
        exit(1);
    }

    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, path, sizeof(addr.sun_path) - 1);

    int attempts = 0;
    while (connect(g_sock_fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        attempts++;
        if (attempts > 50) {
            fprintf(stderr,
                "[fafnir_dpi_bridge_axi] connect() failed after retries: %s\n"
                "  (socket path: %s -- is axi_bridge_server.py running?)\n",
                strerror(errno), path);
            exit(1);
        }
        usleep(100000);
    }

    g_connected = 1;
    fprintf(stderr, "[fafnir_dpi_bridge_axi] connected to %s\n", path);
}

static void send_all(const void *buf, size_t len) {
    const char *p = (const char *)buf;
    size_t sent = 0;
    while (sent < len) {
        ssize_t n = write(g_sock_fd, p + sent, len - sent);
        if (n <= 0) {
            perror("[fafnir_dpi_bridge_axi] write() failed");
            exit(1);
        }
        sent += (size_t)n;
    }
}

static void recv_all(void *buf, size_t len) {
    char *p = (char *)buf;
    size_t got = 0;
    while (got < len) {
        ssize_t n = read(g_sock_fd, p + got, len - got);
        if (n <= 0) {
            perror("[fafnir_dpi_bridge_axi] read() failed (or peer closed)");
            exit(1);
        }
        got += (size_t)n;
    }
}

void fafnir_axi_step(
    int reg0, int reg1, int reg2, int reg3,
    int last_rdata, int last_bresp, int last_rresp,
    int assertion_fail_count,
    unsigned char *reset_req, int *op, int *addr, int *wdata, int *wstrb,
    unsigned char *stop_req
) {
    if (!g_connected) {
        fafnir_axi_connect();
    }

    axi_state_msg_t out_msg;
    out_msg.reg0 = (int32_t)reg0;
    out_msg.reg1 = (int32_t)reg1;
    out_msg.reg2 = (int32_t)reg2;
    out_msg.reg3 = (int32_t)reg3;
    out_msg.last_rdata = (int32_t)last_rdata;
    out_msg.last_bresp = (int32_t)last_bresp;
    out_msg.last_rresp = (int32_t)last_rresp;
    out_msg.assertion_fail_count = (int32_t)assertion_fail_count;

    send_all(&out_msg, sizeof(out_msg));

    axi_action_msg_t in_msg;
    recv_all(&in_msg, sizeof(in_msg));

    *reset_req = (unsigned char)(in_msg.reset_req ? 1 : 0);
    *op        = (int)in_msg.op;
    *addr      = (int)in_msg.addr;
    *wdata     = (int)in_msg.wdata;
    *wstrb     = (int)in_msg.wstrb;
    *stop_req  = (unsigned char)(in_msg.stop_req ? 1 : 0);
}
