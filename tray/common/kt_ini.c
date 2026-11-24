/* SPDX-License-Identifier: MIT
 * Author: Peter Kokosharov <peter.kokosharov@gmail.com>
 * AI models: claude-sonnet-5-5
 */
#include "kt_ini.h"

#include <stdlib.h>
#include <string.h>

#include "kt_util.h"

static int is_sp(char c) { return c == ' ' || c == '\t'; }

char *kt_ini_get(const char *text, const char *key)
{
    size_t klen;
    const char *line;

    if (!text || !key || !key[0]) return NULL;
    klen = strlen(key);
    if ((unsigned char)text[0] == 0xEF && (unsigned char)text[1] == 0xBB && (unsigned char)text[2] == 0xBF)
        text += 3;
    for (line = text; *line; ) {
        const char *eol = line, *k, *v, *end;
        while (*eol && *eol != '\n' && *eol != '\r') eol++;
        k = line;
        while (k < eol && is_sp(*k)) k++;
        if (k < eol && *k != '#' && *k != ';' &&
            (size_t)(eol - k) >= klen && kt_strncasecmp(k, key, klen) == 0) {
            v = k + klen;
            while (v < eol && is_sp(*v)) v++;
            if (v < eol && *v == '=') {
                v++;
                while (v < eol && is_sp(*v)) v++;
                end = eol;
                while (end > v && is_sp(end[-1])) end--;
                {
                    char *r = (char *)malloc((size_t)(end - v) + 1);
                    if (!r) return NULL;
                    memcpy(r, v, (size_t)(end - v));
                    r[end - v] = '\0';
                    return r;
                }
            }
        }
        line = eol;
        while (*line == '\n' || *line == '\r') line++;
    }
    return NULL;
}

char *kt_expand_vars(const char *s, kt_lookup_fn lookup, void *ctx)
{
    kt_sb sb;
    if (!s) return NULL;
    kt_sb_init(&sb);
    while (*s) {
        const char *close = (*s == '%') ? strchr(s + 1, '%') : NULL;
        if (close && close > s + 1 && close - s - 1 < 128) {
            char name[128];
            const char *val;
            size_t n = (size_t)(close - s - 1);
            memcpy(name, s + 1, n);
            name[n] = '\0';
            val = lookup ? lookup(name, ctx) : NULL;
            if (val) {
                kt_sb_append(&sb, val);
                s = close + 1;
                continue;
            }
        }
        kt_sb_appendn(&sb, s, 1);
        s++;
    }
    if (sb.oom) { kt_sb_free(&sb); return NULL; }
    return kt_sb_take(&sb);
}
