/* SPDX-License-Identifier: MIT
 * Author: Peter Kokosharov <peter.kokosharov@gmail.com>
 * kt_json.h - small NULL-safe accessors over cJSON (MIT, Dave Gamble; native/third_party/cjson).
 * The parser itself is cJSON; nothing here parses text.  All accessors tolerate NULL and a wrong
 * type and then return the default, so callers need no null checks on API replies.
 */
#ifndef KT_JSON_H
#define KT_JSON_H

#include <stddef.h>

#include "cJSON.h"

typedef cJSON kt_json;

/* Parses exactly one JSON value spanning the whole buffer; NULL on any error. */
kt_json       *kt_json_parse(const char *text, size_t len);
void           kt_json_free(kt_json *j);

const kt_json *kt_json_get(const kt_json *obj, const char *key);     /* NULL unless obj is an object */
const kt_json *kt_json_at(const kt_json *arr, size_t i);              /* NULL unless arr is an array */
size_t         kt_json_count(const kt_json *arr_or_obj);
const char    *kt_json_string(const kt_json *j, const char *dflt);
double         kt_json_number(const kt_json *j, double dflt);
int            kt_json_bool(const kt_json *j, int dflt);
int            kt_json_is_bool(const kt_json *j);
int            kt_json_is_number(const kt_json *j);

#endif
