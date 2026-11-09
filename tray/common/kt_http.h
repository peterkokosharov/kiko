/* SPDX-License-Identifier: MIT
 * Author: Peter Kokosharov <peter.kokosharov@gmail.com>
 * kt_http.h - minimal HTTP/1.1 client for 127.0.0.1 ONLY (no TLS, no redirects, no keep-alive).
 * One request per connection, "Connection: close", the response is read to EOF (max 4 MiB).
 * Blocking: call it from the worker thread, never from the UI thread.
 */
#ifndef KT_HTTP_H
#define KT_HTTP_H

#include <stddef.h>

#include "kt_util.h"

typedef struct {
    int    status;     /* HTTP status code, 0 if none */
    char  *body;       /* malloc'd, NUL-terminated, never NULL after a successful request */
    size_t body_len;
} kt_http_resp;

/* Return codes of kt_http_request. 0 = a response was received (any HTTP status). */
#define KT_HTTP_OK          0
#define KT_HTTP_ERR_CONNECT (-1)   /* nothing listening / network down */
#define KT_HTTP_ERR_IO      (-2)   /* send/recv failed or timed out */
#define KT_HTTP_ERR_PROTO   (-3)   /* the reply was not valid HTTP */
#define KT_HTTP_ERR_ARG     (-4)   /* bad method/path/token (e.g. contains CR, LF or a space) */

/* Signature used by the presenter, so tests can inject a fake. */
typedef int (*kt_http_fn)(const char *method, int port, const char *path, const char *bearer,
                          const char *json_body, int timeout_ms, kt_http_resp *out);

/* One-time network init (Winsock WSAStartup; no-op elsewhere).  Call from main before threads start. */
int kt_net_startup(void);

/* Builds the request text.  Returns 0, or KT_HTTP_ERR_ARG.  Pure, unit tested. */
int kt_http_build_request(kt_sb *out, const char *method, int port, const char *path,
                          const char *bearer, const char *json_body);

/* Parses a complete response read to EOF.  Handles Content-Length, chunked and close-delimited
 * bodies.  Returns 0 or KT_HTTP_ERR_PROTO.  Pure, unit tested. */
int kt_http_parse_response(const char *buf, size_t len, kt_http_resp *out);

/* The real client.  out is zeroed first; on 0 the caller frees it with kt_http_resp_free. */
int  kt_http_request(const char *method, int port, const char *path, const char *bearer,
                     const char *json_body, int timeout_ms, kt_http_resp *out);
void kt_http_resp_free(kt_http_resp *r);

#endif
