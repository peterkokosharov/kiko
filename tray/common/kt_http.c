/* SPDX-License-Identifier: MIT
 * Author: Peter Kokosharov <peter.kokosharov@gmail.com>
 */
#include "kt_http.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <winsock2.h>
#include <ws2tcpip.h>
typedef SOCKET kt_sock;
#define KT_BAD_SOCK INVALID_SOCKET
#define kt_close_sock closesocket
#else
#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>
typedef int kt_sock;
#define KT_BAD_SOCK (-1)
#define kt_close_sock close
#endif

#define KT_HTTP_MAX_RESPONSE (4u * 1024u * 1024u)

int kt_net_startup(void)
{
#ifdef _WIN32
    static int done = 0;
    WSADATA d;
    if (done) return 0;
    if (WSAStartup(MAKEWORD(2, 2), &d) != 0) return -1;
    done = 1;
#endif
    return 0;
}

static int has_bad_char(const char *s, int allow_space)
{
    for (; *s; s++) {
        unsigned char c = (unsigned char)*s;
        if (c < 0x20 || c == 0x7F) return 1;
        if (c == ' ' && !allow_space) return 1;
    }
    return 0;
}

int kt_http_build_request(kt_sb *out, const char *method, int port, const char *path,
                          const char *bearer, const char *json_body)
{
    if (!method || !path || port < 1 || port > 65535) return KT_HTTP_ERR_ARG;
    if (strcmp(method, "GET") != 0 && strcmp(method, "POST") != 0) return KT_HTTP_ERR_ARG;
    if (path[0] != '/' || has_bad_char(path, 0)) return KT_HTTP_ERR_ARG;
    if (bearer && (bearer[0] == '\0' || has_bad_char(bearer, 0))) return KT_HTTP_ERR_ARG;

    kt_sb_printf(out, "%s %s HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n", method, path, port);
    kt_sb_append(out, "User-Agent: kiko-tray\r\nAccept: application/json\r\nConnection: close\r\n");
    if (bearer) kt_sb_printf(out, "Authorization: Bearer %s\r\n", bearer);
    if (json_body)
        kt_sb_printf(out, "Content-Type: application/json\r\nContent-Length: %lu\r\n",
                     (unsigned long)strlen(json_body));
    else if (strcmp(method, "POST") == 0)
        kt_sb_append(out, "Content-Length: 0\r\n");
    kt_sb_append(out, "\r\n");
    if (json_body) kt_sb_append(out, json_body);
    return out->oom ? KT_HTTP_ERR_IO : 0;
}

/* Finds needle (ASCII) in the first n bytes of s; NULL if absent. */
static const char *find_bytes(const char *s, size_t n, const char *needle)
{
    size_t m = strlen(needle), i;
    if (m == 0 || n < m) return NULL;
    for (i = 0; i + m <= n; i++)
        if (memcmp(s + i, needle, m) == 0) return s + i;
    return NULL;
}

/* Value of header name (without colon) in the header block of n bytes at h.  Returns a pointer to
 * the first non-blank value byte and sets *vlen, or NULL. */
static const char *header_value(const char *h, size_t n, const char *name, size_t *vlen)
{
    size_t nl = strlen(name);
    const char *end = h + n;
    const char *line = h;
    while (line < end) {
        const char *eol = find_bytes(line, (size_t)(end - line), "\r\n");
        size_t ll = eol ? (size_t)(eol - line) : (size_t)(end - line);
        if (ll > nl && line[nl] == ':' && kt_strncasecmp(line, name, nl) == 0) {
            const char *v = line + nl + 1;
            const char *ve = line + ll;
            while (v < ve && (*v == ' ' || *v == '\t')) v++;
            while (ve > v && (ve[-1] == ' ' || ve[-1] == '\t')) ve--;
            *vlen = (size_t)(ve - v);
            return v;
        }
        if (!eol) break;
        line = eol + 2;
    }
    return NULL;
}

static int hexval(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

static int decode_chunked(const char *b, size_t n, kt_sb *out)
{
    size_t pos = 0;
    for (;;) {
        size_t size = 0;
        int digits = 0;
        while (pos < n && hexval(b[pos]) >= 0) {
            size = size * 16 + (size_t)hexval(b[pos++]);
            digits++;
            if (size > KT_HTTP_MAX_RESPONSE) return -1;
        }
        if (!digits) return -1;
        while (pos < n && b[pos] != '\n') pos++;   /* skip chunk extensions and the CR */
        if (pos >= n) return -1;
        pos++;
        if (size == 0) return 0;
        if (n - pos < size) return -1;
        kt_sb_appendn(out, b + pos, size);
        pos += size;
        if (n - pos < 2 || b[pos] != '\r' || b[pos + 1] != '\n') return -1;
        pos += 2;
    }
}

int kt_http_parse_response(const char *buf, size_t len, kt_http_resp *out)
{
    const char *hend, *eol, *body, *v;
    size_t hlen, blen, vlen;
    int status;

    out->status = 0;
    out->body = NULL;
    out->body_len = 0;
    hend = find_bytes(buf, len, "\r\n\r\n");
    if (!hend) return KT_HTTP_ERR_PROTO;
    hlen = (size_t)(hend - buf);
    body = hend + 4;
    blen = len - hlen - 4;

    eol = find_bytes(buf, hlen, "\r\n");
    if (!eol) eol = hend;
    if ((size_t)(eol - buf) < 12 || memcmp(buf, "HTTP/1.", 7) != 0 || buf[8] != ' ')
        return KT_HTTP_ERR_PROTO;
    if (hexval(buf[9]) < 0 || hexval(buf[9]) > 9 || hexval(buf[10]) < 0 || hexval(buf[10]) > 9
        || hexval(buf[11]) < 0 || hexval(buf[11]) > 9)
        return KT_HTTP_ERR_PROTO;
    status = (buf[9] - '0') * 100 + (buf[10] - '0') * 10 + (buf[11] - '0');

    v = header_value(buf, hlen, "Transfer-Encoding", &vlen);
    if (v && vlen >= 7 && kt_strncasecmp(v, "chunked", 7) == 0) {
        kt_sb sb;
        kt_sb_init(&sb);
        if (decode_chunked(body, blen, &sb) != 0) { kt_sb_free(&sb); return KT_HTTP_ERR_PROTO; }
        out->body_len = sb.len;
        out->body = kt_sb_take(&sb);
    } else {
        v = header_value(buf, hlen, "Content-Length", &vlen);
        if (v) {
            size_t cl = 0, i;
            if (vlen == 0 || vlen > 9) return KT_HTTP_ERR_PROTO;
            for (i = 0; i < vlen; i++) {
                if (hexval(v[i]) < 0 || hexval(v[i]) > 9) return KT_HTTP_ERR_PROTO;
                cl = cl * 10 + (size_t)(v[i] - '0');
            }
            if (cl < blen) blen = cl;   /* ignore trailing garbage; a short body is kept as received */
        }
        out->body = (char *)malloc(blen + 1);
        if (out->body) {
            memcpy(out->body, body, blen);
            out->body[blen] = '\0';
            out->body_len = blen;
        }
    }
    if (!out->body) return KT_HTTP_ERR_PROTO;
    out->status = status;
    return 0;
}

void kt_http_resp_free(kt_http_resp *r)
{
    if (!r) return;
    free(r->body);
    r->body = NULL;
    r->body_len = 0;
    r->status = 0;
}

static void set_timeouts(kt_sock s, int timeout_ms)
{
#ifdef _WIN32
    DWORD t = (DWORD)timeout_ms;
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, (const char *)&t, sizeof t);
    setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, (const char *)&t, sizeof t);
#else
    struct timeval tv;
    tv.tv_sec = timeout_ms / 1000;
    tv.tv_usec = (timeout_ms % 1000) * 1000;
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
    setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);
#endif
}

int kt_http_request(const char *method, int port, const char *path, const char *bearer,
                    const char *json_body, int timeout_ms, kt_http_resp *out)
{
    kt_sb req, resp;
    struct sockaddr_in addr;
    kt_sock s;
    size_t sent = 0;
    int rc;
    char chunk[4096];

    out->status = 0;
    out->body = NULL;
    out->body_len = 0;
    kt_sb_init(&req);
    rc = kt_http_build_request(&req, method, port, path, bearer, json_body);
    if (rc != 0) { kt_sb_free(&req); return rc; }
    if (kt_net_startup() != 0) { kt_sb_free(&req); return KT_HTTP_ERR_CONNECT; }

    s = socket(AF_INET, SOCK_STREAM, 0);
    if (s == KT_BAD_SOCK) { kt_sb_free(&req); return KT_HTTP_ERR_CONNECT; }
#ifdef SO_NOSIGPIPE
    { int one = 1; setsockopt(s, SOL_SOCKET, SO_NOSIGPIPE, &one, sizeof one); }   /* macOS: no MSG_NOSIGNAL */
#endif
    set_timeouts(s, timeout_ms > 0 ? timeout_ms : 3000);
    memset(&addr, 0, sizeof addr);
    addr.sin_family = AF_INET;
    addr.sin_port = htons((unsigned short)port);
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);   /* loopback only, by design */
    if (connect(s, (struct sockaddr *)&addr, (int)sizeof addr) != 0) {
        kt_close_sock(s);
        kt_sb_free(&req);
        return KT_HTTP_ERR_CONNECT;
    }

    while (sent < req.len) {
#ifdef MSG_NOSIGNAL
        int n = (int)send(s, req.p + sent, (int)(req.len - sent), MSG_NOSIGNAL);
#else
        int n = (int)send(s, req.p + sent, (int)(req.len - sent), 0);
#endif
        if (n <= 0) { kt_close_sock(s); kt_sb_free(&req); return KT_HTTP_ERR_IO; }
        sent += (size_t)n;
    }
    kt_sb_free(&req);

    kt_sb_init(&resp);
    for (;;) {
        int n = (int)recv(s, chunk, (int)sizeof chunk, 0);
        if (n == 0) break;
        if (n < 0 || resp.len + (size_t)n > KT_HTTP_MAX_RESPONSE) {
            rc = n < 0 ? KT_HTTP_ERR_IO : KT_HTTP_ERR_PROTO;
            kt_sb_free(&resp);
            kt_close_sock(s);
            return rc;
        }
        kt_sb_appendn(&resp, chunk, (size_t)n);
    }
    kt_close_sock(s);
    if (resp.oom || !resp.p) { kt_sb_free(&resp); return KT_HTTP_ERR_IO; }
    rc = kt_http_parse_response(resp.p, resp.len, out);
    kt_sb_free(&resp);
    return rc;
}
