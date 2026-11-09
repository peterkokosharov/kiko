/* SPDX-License-Identifier: MIT
 * Author: Peter Kokosharov <peter.kokosharov@gmail.com>
 * kt_http_request against a tiny loopback server running in a thread.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "kt_http.h"
#include "kt_test.h"
#include "kt_thread.h"

#ifdef _WIN32
#include <ws2tcpip.h>
typedef SOCKET sock_t;
#define BAD_SOCK INVALID_SOCKET
#define closesock closesocket
typedef int socklen_;
#else
#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>
typedef int sock_t;
#define BAD_SOCK (-1)
#define closesock close
typedef socklen_t socklen_;
#endif

static const char *REPLIES[3] = {
    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 15\r\n\r\n{\"status\":\"ok\"}",
    "HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n4\r\nabcd\r\n0\r\n\r\n",
    "garbage that is not http"
};

static sock_t g_listen;
static char   g_seen[3][512];     /* what the client sent, per connection */

static void server_main(void *arg)
{
    int i;
    (void)arg;
    for (i = 0; i < 3; i++) {
        sock_t c = accept(g_listen, NULL, NULL);
        int n, total = 0;
        if (c == BAD_SOCK) return;
        while (total < (int)sizeof g_seen[i] - 1) {
            n = (int)recv(c, g_seen[i] + total, (int)sizeof g_seen[i] - 1 - total, 0);
            if (n <= 0) break;
            total += n;
            g_seen[i][total] = '\0';
            if (strstr(g_seen[i], "\r\n\r\n")) break;
        }
        send(c, REPLIES[i], (int)strlen(REPLIES[i]), 0);
        closesock(c);
    }
}

int main(void)
{
    struct sockaddr_in a;
    socklen_ len = (socklen_)sizeof a;
    kt_thread th;
    kt_http_resp r;
    int port, rc;

    CHECK_INT(kt_net_startup(), 0);
    g_listen = socket(AF_INET, SOCK_STREAM, 0);
    CHECK(g_listen != BAD_SOCK);
    memset(&a, 0, sizeof a);
    a.sin_family = AF_INET;
    a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    a.sin_port = 0;
    CHECK_INT(bind(g_listen, (struct sockaddr *)&a, (int)sizeof a), 0);
    CHECK_INT(listen(g_listen, 4), 0);
    CHECK_INT(getsockname(g_listen, (struct sockaddr *)&a, &len), 0);
    port = ntohs(a.sin_port);
    CHECK(kt_thread_start(&th, server_main, NULL) == 0);

    rc = kt_http_request("GET", port, "/health", NULL, NULL, 3000, &r);
    CHECK_INT(rc, 0);
    CHECK_INT(r.status, 200);
    CHECK_STR(r.body, "{\"status\":\"ok\"}");
    kt_http_resp_free(&r);
    CHECK(strncmp(g_seen[0], "GET /health HTTP/1.1\r\n", 22) == 0);
    CHECK(strstr(g_seen[0], "Authorization") == NULL);

    rc = kt_http_request("GET", port, "/kiko/v1/models", "tok", NULL, 3000, &r);
    CHECK_INT(rc, 0);
    CHECK_STR(r.body, "abcd");
    kt_http_resp_free(&r);
    CHECK(strstr(g_seen[1], "Authorization: Bearer tok\r\n") != NULL);

    rc = kt_http_request("GET", port, "/x", NULL, NULL, 3000, &r);
    CHECK_INT(rc, KT_HTTP_ERR_PROTO);

    kt_thread_join(&th);
    closesock(g_listen);

    /* nothing listens any more: connection refused is reported as such */
    rc = kt_http_request("GET", port, "/health", NULL, NULL, 1000, &r);
    CHECK_INT(rc, KT_HTTP_ERR_CONNECT);
    CHECK_INT(kt_http_request("GET", port, "/a b", NULL, NULL, 1000, &r), KT_HTTP_ERR_ARG);

    KT_TEST_DONE();
}
