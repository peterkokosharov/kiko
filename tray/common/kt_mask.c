/* SPDX-License-Identifier: MIT
 * Author: Peter Kokosharov <peter.kokosharov@gmail.com>
 */
#include "kt_mask.h"

#include <stdio.h>
#include <string.h>

#include "kt_util.h"

int kt_mask_name_ok(const char *name)
{
    size_t i;
    if (!name || !name[0]) return 0;
    for (i = 0; name[i]; i++) {
        char c = name[i];
        int alpha = (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || c == '_';
        int digit = c >= '0' && c <= '9';
        if (i >= 64) return 0;
        if (!(alpha || (digit && i > 0))) return 0;
    }
    return 1;
}

int kt_mask_key_label(char *out, size_t cap, const char *name)
{
    int ok = kt_mask_name_ok(name);
    int n;
    if (cap == 0) return -1;
    n = snprintf(out, cap, "%s: *****", ok ? name : "(invalid key name)");
    if (n < 0 || (size_t)n >= cap) { out[0] = '\0'; return -1; }
    return ok ? 0 : -1;
}

const char *kt_mask_status_word(int present) { return present ? "present" : "missing"; }

static int is_tok(char c)
{
    return (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9')
           || c == '-' || c == '_' || c == '.' || c == '~' || c == '+' || c == '/' || c == '=';
}

char *kt_mask_redact(const char *text)
{
    kt_sb sb;
    size_t i = 0, n;
    if (!text) return NULL;
    n = strlen(text);
    kt_sb_init(&sb);
    while (i < n) {
        size_t j;
        /* "Bearer <token>" */
        if (kt_strncasecmp(text + i, "bearer ", 7) == 0 && (i == 0 || !is_tok(text[i - 1]))) {
            kt_sb_appendn(&sb, text + i, 7);
            j = i + 7;
            while (j < n && is_tok(text[j])) j++;
            if (j > i + 7) kt_sb_append(&sb, "[REDACTED]");
            i = j;
            continue;
        }
        /* sk-XXXXXXXX... style keys */
        if (kt_strncasecmp(text + i, "sk-", 3) == 0 && (i == 0 || !is_tok(text[i - 1]))) {
            j = i + 3;
            while (j < n && is_tok(text[j])) j++;
            if (j - (i + 3) >= 8) {
                kt_sb_append(&sb, "[REDACTED]");
                i = j;
                continue;
            }
        }
        /* api_key=VALUE, api_key: VALUE, with optional quotes around name and value */
        if (kt_strncasecmp(text + i, "api_key", 7) == 0) {
            j = i + 7;
            if (j < n && text[j] == 0x22) j++;
            while (j < n && text[j] == ' ') j++;
            if (j < n && (text[j] == '=' || text[j] == ':')) {
                j++;
                while (j < n && text[j] == ' ') j++;
                kt_sb_appendn(&sb, text + i, j - i);
                if (j < n && text[j] == 0x22) { kt_sb_appendn(&sb, text + j, 1); j++; }
                if (j < n && is_tok(text[j])) {
                    while (j < n && is_tok(text[j])) j++;
                    kt_sb_append(&sb, "[REDACTED]");
                }
                i = j;
                continue;
            }
        }
        kt_sb_appendn(&sb, text + i, 1);
        i++;
    }
    return kt_sb_take(&sb);
}
