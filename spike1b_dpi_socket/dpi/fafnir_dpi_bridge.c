/*
 * fafnir_dpi_bridge.c -- Spike 1B: DPI-C <-> Unix domain socket bridge.
 *
 * This is the C-side counterpart to fifo_tb_top.sv's `fafnir_step` import.
 * It owns a Unix domain socket connection to a separate Python process and
 * implements a trivial fixed-format wire protocol:
 *
 *   SV -> Python (state message), 20 bytes:
 *     int32  count
 *     int32  full        (0 or 1)
 *     int32  empty       (0 or 1)
 *     int32  pop_data
 *     int32  assertion_fail_count   (added when the assertion-visibility
 *                                    gap was identified during Phase 3
 *                                    reward-aggregator scoping; was
 *                                    previously absent from this struct
 *                                    entirely, not just unused)
 *
 *   Python -> SV (action message), 16 bytes:
 *     int32  reset_req    (0 or 1)
 *     int32  op           (0=noop, 1=push, 2=pop)
 *     int32  push_data
 *     int32  stop_req     (0 or 1)
 *
 * Fixed-width binary, no framing/length prefix needed since every message
 * is a fixed size. NOTE: state and action messages are NO LONGER the same
 * size (20 vs 16 bytes) after this update -- they happened to both be 16
 * bytes before assertion_fail_count was added. Any code that assumed
 * symmetric sizes (there was none, but it would have been an easy
 * assumption to make) would break here. Flagging since this is exactly
 * the kind of two-language-struct-sync risk noted as a Spike 1B cost in
 * the Phase 1 work plan findings.
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

#define DEFAULT_SOCKET_PATH "/tmp/fafnir_spike1b.sock"

typedef struct {
    int32_t count;
    int32_t full;
    int32_t empty;
    int32_t pop_data;
    int32_t assertion_fail_count;
} state_msg_t;   /* 20 bytes */

typedef struct {
    int32_t reset_req;
    int32_t op;
    int32_t push_data;
    int32_t stop_req;
} action_msg_t;  /* 16 bytes */

static int g_sock_fd = -1;
static int g_connected = 0;

static void fafnir_connect(void) {
    const char *path = getenv("FAFNIR_SOCKET_PATH");
    if (path == NULL) path = DEFAULT_SOCKET_PATH;

    g_sock_fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (g_sock_fd < 0) {
        perror("[fafnir_dpi_bridge] socket() failed");
        exit(1);
    }

    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, path, sizeof(addr.sun_path) - 1);

    /*
     * Retry connect for a few seconds in case the Python listener hasn't
     * bound yet -- avoids a brittle race where the simulator starts
     * faster than the Python harness.
     */
    int attempts = 0;
    while (connect(g_sock_fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        attempts++;
        if (attempts > 50) {
            fprintf(stderr,
                "[fafnir_dpi_bridge] connect() failed after retries: %s\n"
                "  (socket path: %s -- is fifo_bridge_server.py running?)\n",
                strerror(errno), path);
            exit(1);
        }
        usleep(100000); /* 100ms */
    }

    g_connected = 1;
    fprintf(stderr, "[fafnir_dpi_bridge] connected to %s\n", path);
}

static void send_all(const void *buf, size_t len) {
    const char *p = (const char *)buf;
    size_t sent = 0;
    while (sent < len) {
        ssize_t n = write(g_sock_fd, p + sent, len - sent);
        if (n <= 0) {
            perror("[fafnir_dpi_bridge] write() failed");
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
            perror("[fafnir_dpi_bridge] read() failed (or peer closed)");
            exit(1);
        }
        got += (size_t)n;
    }
}

/*
 * fafnir_step -- called once per clock cycle from fifo_tb_top.sv.
 *
 * Sends current DUT state to Python, blocks for the next action, returns
 * it via the output arguments. This is the single round-trip that Spike
 * 1B's latency measurement times -- directly comparable to Spike 1A's
 * inject_stimulus() + get_state() pair.
 */
void fafnir_step(
    int count, unsigned char full, unsigned char empty, int pop_data,
    int assertion_fail_count,
    unsigned char *reset_req, int *op, int *push_data, unsigned char *stop_req
) {
    if (!g_connected) {
        fafnir_connect();
    }

    state_msg_t out_msg;
    out_msg.count                = (int32_t)count;
    out_msg.full                 = (int32_t)(full ? 1 : 0);
    out_msg.empty                = (int32_t)(empty ? 1 : 0);
    out_msg.pop_data              = (int32_t)pop_data;
    out_msg.assertion_fail_count = (int32_t)assertion_fail_count;

    send_all(&out_msg, sizeof(out_msg));

    action_msg_t in_msg;
    recv_all(&in_msg, sizeof(in_msg));

    *reset_req = (unsigned char)(in_msg.reset_req ? 1 : 0);
    *op        = (int)in_msg.op;
    *push_data = (int)in_msg.push_data;
    *stop_req  = (unsigned char)(in_msg.stop_req ? 1 : 0);
}
