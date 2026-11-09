/* SPDX-License-Identifier: MIT
 * Author: Peter Kokosharov <peter.kokosharov@gmail.com>
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

/* Overwrites n bytes in a way the compiler may not optimise away (for tokens). */
void  kt_secure_zero(void *p, size_t n);

#endif
