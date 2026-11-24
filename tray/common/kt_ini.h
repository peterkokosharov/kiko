/* SPDX-License-Identifier: MIT
 * Author: Peter Kokosharov <peter.kokosharov@gmail.com>
 * AI models: claude-sonnet-5-5
 * kt_ini.h - a tiny key=value parser (kiko-tray.ini) and %VAR% expansion.  UTF-8, no sections.
 */
#ifndef KT_INI_H
#define KT_INI_H

#include <stddef.h>

/* Looks up key (case-insensitive) in INI text: one "key = value" per line; lines starting with '#' or
 * ';' are comments; a UTF-8 BOM, CR LF and spaces around key and value are tolerated.  The value is
 * taken verbatim (quotes are kept, so a command line with quotes works).  The first match wins.
 * Returns a malloc'd string (possibly empty) or NULL when the key is absent or on out of memory. */
char *kt_ini_get(const char *text, const char *key);

/* Replaces each %NAME% by lookup(NAME, ctx) (a pointer valid until the next call, or NULL = unknown;
 * an unknown name is left as written).  Returns a malloc'd string, NULL on out of memory. */
typedef const char *(*kt_lookup_fn)(const char *name, void *ctx);
char *kt_expand_vars(const char *s, kt_lookup_fn lookup, void *ctx);

#endif
