/* SPDX-License-Identifier: MIT
 * Author: Peter Kokosharov <peter.kokosharov@gmail.com>
 */
#include "kt_json.h"

kt_json *kt_json_parse(const char *text, size_t len)
{
    const char *end = NULL;
    if (!text) return NULL;
    return cJSON_ParseWithLengthOpts(text, len, &end, 1);
}

void kt_json_free(kt_json *j) { cJSON_Delete(j); }

const kt_json *kt_json_get(const kt_json *obj, const char *key)
{
    if (!obj || !key || !cJSON_IsObject(obj)) return NULL;
    return cJSON_GetObjectItemCaseSensitive(obj, key);
}

const kt_json *kt_json_at(const kt_json *arr, size_t i)
{
    if (!arr || !cJSON_IsArray(arr) || i > 0x7FFFFFFFu) return NULL;
    return cJSON_GetArrayItem(arr, (int)i);
}

size_t kt_json_count(const kt_json *j)
{
    if (!j || (!cJSON_IsArray(j) && !cJSON_IsObject(j))) return 0;
    return (size_t)cJSON_GetArraySize(j);
}

const char *kt_json_string(const kt_json *j, const char *dflt)
{
    return (j && cJSON_IsString(j) && j->valuestring) ? j->valuestring : dflt;
}

double kt_json_number(const kt_json *j, double dflt)
{
    return (j && cJSON_IsNumber(j)) ? j->valuedouble : dflt;
}

int kt_json_bool(const kt_json *j, int dflt)
{
    return (j && cJSON_IsBool(j)) ? cJSON_IsTrue(j) : dflt;
}

int kt_json_is_bool(const kt_json *j) { return j && cJSON_IsBool(j); }
int kt_json_is_number(const kt_json *j) { return j && cJSON_IsNumber(j); }
