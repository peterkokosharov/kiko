/* SPDX-License-Identifier: MIT
 * Author: Peter Kokosharov <peter.kokosharov@gmail.com>
 * AI models: unknown (before 2026-11-24), claude-sonnet-5-5
 * kt_util.h - tiny string helpers shared by the whole tray (UTF-8 everywhere).
 */
#ifndef KT_UTIL_H
#define KT_UTIL_H

#include <stddef.h>

/* malloc'd copy; NULL in -> NULL out; NULL on out of memory. */
char *kt_strdup(const char *s);
/* Case-insensitive ASCII compare of at most n bytes (0 = equal). */
int kt_strncasecmp(const char *a, const char *b, size_t n);

/* Growable string builder.  NUL-terminated once anything was appended. */
typedef struct {
    char  *p;
    size_t len;
    size_t cap;
    int    oom;   /* set once an allocation failed; further appends are ignored */
} kt_sb;

void  kt_sb_init(kt_sb *sb);
void  kt_sb_append(kt_sb *sb, const char *s);
void  kt_sb_appendn(kt_sb *sb, const char *s, size_t n);
void  kt_sb_printf(kt_sb *sb, const char *fmt, ...);
/* Hands the buffer to the caller (free() it) and resets the builder.  An empty builder yields "".
 * NULL only on out of memory. */
char *kt_sb_take(kt_sb *sb);
void  kt_sb_free(kt_sb *sb);

/* Builds the text of a log dialog, newest entry first.  lines[0..n) are the log lines oldest-first.  An entry
 * starts at a line that begins with a date ("YYYY-MM-DD " or ISO 8601 "YYYY-MM-DDT") or with the server start banner ("kiko-lite on ");
 * every other line (e.g. a traceback) continues the entry above it.  Entries are reversed, the lines inside an
 * entry keep their order, so a traceback still reads top-down.  Lines before the first entry start (a cut-off
 * head) form the oldest group and go last.  If no line is recognised as an entry start, plain line reversal.  Each line is followed by a newline.  Returns malloc'd text ("" for none);
 * NULL only on out of memory. */
char *kt_log_newest_first(const char *const *lines, size_t n);

/* Overwrites n bytes in a way the compiler may not optimise away (for tokens). */
void  kt_secure_zero(void *p, size_t n);

#endif
