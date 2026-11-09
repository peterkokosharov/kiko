/* SPDX-License-Identifier: MIT
 * Author: Peter Kokosharov <peter.kokosharov@gmail.com>
 * Pure tests: request building and response parsing (no sockets).
 */
#include <stdlib.h>
#include <string.h>

#include "kt_http.h"
#include "kt_test.h"

static int parse(const char *raw, kt_http_resp *r) { return kt_http_parse_response(raw, strlen(raw), r); }

int main(void)
{
    kt_sb sb;
    kt_http_resp r;

    /* request building */
    kt_sb_init(&sb);
    CHECK_INT(kt_http_build_request(&sb, "GET", 4000, "/health", NULL, NULL), 0);
    CHECK(strncmp(sb.p, "GET /health HTTP/1.1\r\nHost: 127.0.0.1:4000\r\n", 44) == 0);
    CHECK(strstr(sb.p, "Connection: close\r\n") != NULL);
    CHECK(strstr(sb.p, "Authorization") == NULL);
    CHECK(sb.len >= 4 && strcmp(sb.p + sb.len - 4, "\r\n\r\n") == 0);
    kt_sb_free(&sb);

    kt_sb_init(&sb);
    CHECK_INT(kt_http_build_request(&sb, "POST", 4000, "/kiko/v1/x", "tok123", "{\"class\":\"OPUS\"}"), 0);
    CHECK(strstr(sb.p, "Authorization: Bearer tok123\r\n") != NULL);
    CHECK(strstr(sb.p, "Content-Length: 16\r\n") != NULL);
    CHECK(strcmp(sb.p + sb.len - 16, "{\"class\":\"OPUS\"}") == 0);
    kt_sb_free(&sb);

    /* header injection and bad arguments are refused */
    kt_sb_init(&sb);
    CHECK_INT(kt_http_build_request(&sb, "GET", 4000, "/a\r\nX: y", NULL, NULL), KT_HTTP_ERR_ARG);
    CHECK_INT(kt_http_build_request(&sb, "GET", 4000, "/a b", NULL, NULL), KT_HTTP_ERR_ARG);
    CHECK_INT(kt_http_build_request(&sb, "GET", 4000, "health", NULL, NULL), KT_HTTP_ERR_ARG);
    CHECK_INT(kt_http_build_request(&sb, "GET", 4000, "/x", "t\r\nX: y", NULL), KT_HTTP_ERR_ARG);
    CHECK_INT(kt_http_build_request(&sb, "GET", 4000, "/x", "has space", NULL), KT_HTTP_ERR_ARG);
    CHECK_INT(kt_http_build_request(&sb, "DELETE", 4000, "/x", NULL, NULL), KT_HTTP_ERR_ARG);
    CHECK_INT(kt_http_build_request(&sb, "GET", 0, "/x", NULL, NULL), KT_HTTP_ERR_ARG);
    CHECK_INT(sb.len, 0);
    kt_sb_free(&sb);

    /* Content-Length body */
    CHECK_INT(parse("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\ncontent-length: 15\r\n\r\n{\"status\":\"ok\"}", &r), 0);
    CHECK_INT(r.status, 200);
    CHECK_STR(r.body, "{\"status\":\"ok\"}");
    CHECK_INT(r.body_len, 15);
    kt_http_resp_free(&r);

    /* Content-Length shorter than the data: extra bytes ignored */
    CHECK_INT(parse("HTTP/1.0 404 Not Found\r\nContent-Length: 2\r\n\r\n{}garbage", &r), 0);
    CHECK_INT(r.status, 404);
    CHECK_STR(r.body, "{}");
    kt_http_resp_free(&r);

    /* no length: body runs to EOF */
    CHECK_INT(parse("HTTP/1.1 200 OK\r\n\r\nhello", &r), 0);
    CHECK_STR(r.body, "hello");
    kt_http_resp_free(&r);

    /* empty body */
    CHECK_INT(parse("HTTP/1.1 204 No Content\r\n\r\n", &r), 0);
    CHECK_INT(r.status, 204);
    CHECK_STR(r.body, "");
    kt_http_resp_free(&r);

    /* chunked, with an extension and upper/lower hex */
    CHECK_INT(parse("HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                    "5;ext=1\r\nhello\r\nA\r\n, world!!!\r\n0\r\n\r\n", &r), 0);
    CHECK_STR(r.body, "hello, world!!!");
    kt_http_resp_free(&r);

    /* malformed */
    CHECK_INT(parse("", &r), KT_HTTP_ERR_PROTO);
    CHECK_INT(parse("HTTP/1.1 200 OK\r\nContent-Length: 2\r\n", &r), KT_HTTP_ERR_PROTO);   /* no blank line */
    CHECK_INT(parse("SMTP/1.1 200 OK\r\n\r\n", &r), KT_HTTP_ERR_PROTO);
    CHECK_INT(parse("HTTP/1.1 2x0 OK\r\n\r\n", &r), KT_HTTP_ERR_PROTO);
    CHECK_INT(parse("HTTP/1.1 200 OK\r\nContent-Length: abc\r\n\r\nxx", &r), KT_HTTP_ERR_PROTO);
    CHECK_INT(parse("HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\nzz\r\n", &r), KT_HTTP_ERR_PROTO);
    CHECK_INT(parse("HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhel", &r), KT_HTTP_ERR_PROTO);

    KT_TEST_DONE();
}
