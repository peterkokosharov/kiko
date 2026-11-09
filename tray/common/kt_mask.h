/* SPDX-License-Identifier: MIT
 * Author: Peter Kokosharov <peter.kokosharov@gmail.com>
 * kt_mask.h - key masking.  The tray NEVER fetches, stores, prints or logs key values: the API only
 * gives key NAMES and a present flag.  These helpers format that and add a last line of defence
 * (redaction) for free text such as log lines.
 */
#ifndef KT_MASK_H
#define KT_MASK_H

#include <stddef.h>

/* 1 if name looks like an environment variable name: [A-Za-z_][A-Za-z0-9_]*, at most 64 chars.
 * Anything else (e.g. a value that slipped into the name field) is rejected. */
int kt_mask_name_ok(const char *name);

/* Writes "NAME: *****" (always five stars, whatever the value) into out.  Returns 0, or -1 if the
 * name is not ok or out is too small (out then holds "(invalid key name): *****"). */
int kt_mask_key_label(char *out, size_t cap, const char *name);

/* "present" or "missing". */
const char *kt_mask_status_word(int present);

/* Returns a malloc'd copy of text where secrets are replaced by [REDACTED]:
 * the token after "Bearer ", values after api_key=/api_key: (also quoted), and sk- keys. */
char *kt_mask_redact(const char *text);

#endif
