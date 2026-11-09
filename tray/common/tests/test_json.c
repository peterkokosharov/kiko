/* SPDX-License-Identifier: MIT
 * Author: Peter Kokosharov <peter.kokosharov@gmail.com>
 * Tests our use of cJSON: the accessors and the reply shapes of docs/API-CONTRACT.md.
 */
#include <stdlib.h>
#include <string.h>

#include "kt_json.h"
#include "kt_test.h"

static kt_json *P(const char *s) { return kt_json_parse(s, strlen(s)); }

int main(void)
{
    kt_json *j;
    const kt_json *m;

    /* GET /kiko/v1/models */
    j = P("{\"models\":[{\"name\":\"opus\",\"class\":\"OPUS\",\"enabled\":true,\"status\":\"ok\"},"
          "{\"name\":\"h\",\"class\":\"HAIKU\",\"enabled\":false,\"status\":\"unknown\"}]}");
    CHECK(j != NULL);
    CHECK_INT(kt_json_count(kt_json_get(j, "models")), 2);
    m = kt_json_at(kt_json_get(j, "models"), 1);
    CHECK_STR(kt_json_string(kt_json_get(m, "name"), "?"), "h");
    CHECK(kt_json_is_bool(kt_json_get(m, "enabled")));
    CHECK_INT(kt_json_bool(kt_json_get(m, "enabled"), 1), 0);
    kt_json_free(j);

    /* GET /kiko/v1/costs */
    j = P("{\"days\":30,\"today_usd\":0.5,\"period_usd\":12.25,\"cap_usd\":null,"
          "\"per_model\":[{\"model\":\"sonnet\",\"usd\":12.25,\"tokens_in\":1000,\"tokens_out\":500}]}");
    CHECK(j != NULL);
    CHECK(kt_json_number(kt_json_get(j, "period_usd"), 0) == 12.25);
    CHECK(!kt_json_is_number(kt_json_get(j, "cap_usd")));
    CHECK_INT((long)kt_json_number(kt_json_get(kt_json_at(kt_json_get(j, "per_model"), 0), "tokens_in"), -1), 1000);
    kt_json_free(j);

    /* error envelope */
    j = P("{\"error\":{\"code\":\"unauthorized\",\"message\":\"bad token\"}}");
    CHECK_STR(kt_json_string(kt_json_get(kt_json_get(j, "error"), "message"), ""), "bad token");
    kt_json_free(j);

    /* unicode escape and /health shapes */
    j = P("{\"status\":\"healthy\",\"x\":\"caf\\u00e9\"}");
    CHECK_STR(kt_json_string(kt_json_get(j, "status"), ""), "healthy");
    CHECK_STR(kt_json_string(kt_json_get(j, "x"), ""), "caf\xc3\xa9");
    kt_json_free(j);

    /* wrong types and NULLs give defaults, never crash */
    j = P("{\"a\":1,\"b\":[1,2],\"c\":\"s\"}");
    CHECK_STR(kt_json_string(kt_json_get(j, "a"), "dflt"), "dflt");
    CHECK(kt_json_number(kt_json_get(j, "c"), 7.0) == 7.0);
    CHECK_INT(kt_json_bool(kt_json_get(j, "a"), 1), 1);
    CHECK(kt_json_get(kt_json_get(j, "b"), "x") == NULL);
    CHECK(kt_json_at(kt_json_get(j, "c"), 0) == NULL);
    CHECK_INT(kt_json_count(NULL), 0);
    CHECK(kt_json_get(NULL, "a") == NULL);
    CHECK(kt_json_at(kt_json_get(j, "b"), 5) == NULL);
    kt_json_free(j);

    /* malformed input */
    CHECK(P("") == NULL);
    CHECK(P("{\"a\":") == NULL);
    CHECK(P("{\"a\":1} trailing") == NULL);
    CHECK(P("[1,2,") == NULL);
    CHECK(P("<html>502 Bad Gateway</html>") == NULL);
    CHECK(kt_json_parse(NULL, 0) == NULL);

    KT_TEST_DONE();
}
