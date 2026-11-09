/* SPDX-License-Identifier: MIT
 * Author: Peter Kokosharov <peter.kokosharov@gmail.com>
 */
#include "kt_util.h"

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

char *kt_strdup(const char *s)
{
    size_t n;
    char *r;
    if (!s) return NULL;
    n = strlen(s) + 1;
    r = (char *)malloc(n);
    if (r) memcpy(r, s, n);
    return r;
}

static int lower(int c) { return (c >= 'A' && c <= 'Z') ? c + 32 : c; }

int kt_strncasecmp(const char *a, const char *b, size_t n)
{
    size_t i;
    for (i = 0; i < n; i++) {
        int ca = lower((unsigned char)a[i]);
        int cb = lower((unsigned char)b[i]);
        if (ca != cb) return ca < cb ? -1 : 1;
        if (ca == 0) return 0;
    }
    return 0;
}

void kt_sb_init(kt_sb *sb)
{
    sb->p = NULL;
    sb->len = 0;
    sb->cap = 0;
    sb->oom = 0;
}

static int sb_reserve(kt_sb *sb, size_t extra)
{
    size_t need = sb->len + extra + 1;
    if (sb->oom) return 0;
    if (need > sb->cap) {
        size_t ncap = sb->cap ? sb->cap : 64;
        char *np;
        while (ncap < need) ncap *= 2;
        np = (char *)malloc(ncap);   /* not realloc: the old block may hold a secret and must be wiped */
        if (!np) { sb->oom = 1; return 0; }
        if (sb->p) { memcpy(np, sb->p, sb->len + 1); kt_secure_zero(sb->p, sb->cap); free(sb->p); }
        sb->p = np;
        sb->cap = ncap;
    }
    return 1;
}

void kt_sb_appendn(kt_sb *sb, const char *s, size_t n)
{
    if (!s || n == 0) return;
    if (!sb_reserve(sb, n)) return;
    memcpy(sb->p + sb->len, s, n);
    sb->len += n;
    sb->p[sb->len] = '\0';
}

void kt_sb_append(kt_sb *sb, const char *s)
{
    if (s) kt_sb_appendn(sb, s, strlen(s));
}

void kt_sb_printf(kt_sb *sb, const char *fmt, ...)
{
    char small[256];
    va_list ap;
    int n;

    va_start(ap, fmt);
    n = vsnprintf(small, sizeof small, fmt, ap);
    va_end(ap);
    if (n < 0) { kt_secure_zero(small, sizeof small); return; }
    if ((size_t)n < sizeof small) {
        kt_sb_appendn(sb, small, (size_t)n);
        kt_secure_zero(small, sizeof small);   /* may have held a header with the token */
        return;
    }
    kt_secure_zero(small, sizeof small);
    if (!sb_reserve(sb, (size_t)n)) return;
    va_start(ap, fmt);
    vsnprintf(sb->p + sb->len, (size_t)n + 1, fmt, ap);
    va_end(ap);
    sb->len += (size_t)n;
}

char *kt_sb_take(kt_sb *sb)
{
    char *r;
    if (sb->oom) { kt_sb_free(sb); return NULL; }
    r = sb->p ? sb->p : kt_strdup("");
    sb->p = NULL;
    sb->len = sb->cap = 0;
    return r;
}

void kt_sb_free(kt_sb *sb)
{
    if (sb->p) kt_secure_zero(sb->p, sb->cap);   /* the buffer may hold an Authorization header */
    free(sb->p);
    kt_sb_init(sb);
}

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
void kt_secure_zero(void *p, size_t n) { SecureZeroMemory(p, n); }
#elif defined(__linux__)
void kt_secure_zero(void *p, size_t n) { explicit_bzero(p, n); }
#else
void kt_secure_zero(void *p, size_t n)
{
    volatile unsigned char *v = (volatile unsigned char *)p;   /* fallback: volatile stores */
    while (n--) *v++ = 0;
}
#endif
