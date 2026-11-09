/* SPDX-License-Identifier: MIT
 * Author: Peter Kokosharov <peter.kokosharov@gmail.com>
 */
#include <stdlib.h>
#include <string.h>

#include "kt_mask.h"
#include "kt_test.h"

static void redact_is(const char *in, const char *expect)
{
    char *r = kt_mask_redact(in);
    CHECK_STR(r, expect);
    free(r);
}

int main(void)
{
    char buf[128];

    CHECK(kt_mask_name_ok("NVIDIA_API_KEY"));
    CHECK(kt_mask_name_ok("_x1"));
    CHECK(!kt_mask_name_ok(""));
    CHECK(!kt_mask_name_ok("1ABC"));
    CHECK(!kt_mask_name_ok("sk-abcdef0123456789"));   /* a value in the name field is rejected */
    CHECK(!kt_mask_name_ok("A B"));
    CHECK(!kt_mask_name_ok(NULL));

    CHECK_INT(kt_mask_key_label(buf, sizeof buf, "NVIDIA_API_KEY"), 0);
    CHECK_STR(buf, "NVIDIA_API_KEY: *****");
    CHECK_INT(kt_mask_key_label(buf, sizeof buf, "sk-abcdef0123456789"), -1);
    CHECK_STR(buf, "(invalid key name): *****");
    CHECK(strstr(buf, "abcdef") == NULL);
    CHECK_INT(kt_mask_key_label(buf, 8, "NVIDIA_API_KEY"), -1);   /* too small */
    CHECK_STR(buf, "");

    CHECK_STR(kt_mask_status_word(1), "present");
    CHECK_STR(kt_mask_status_word(0), "missing");

    redact_is("Authorization: Bearer abc.DEF-123 ok", "Authorization: Bearer [REDACTED] ok");
    redact_is("key sk-ant-api03-ABCDEFGH1234 end", "key [REDACTED] end");
    redact_is("sk-short", "sk-short");
    redact_is("url?api_key=SECRET123&x=1", "url?api_key=[REDACTED]&x=1");
    redact_is("{\"api_key\": \"topsecret\"}", "{\"api_key\": \"[REDACTED]\"}");
    redact_is("nothing to hide", "nothing to hide");
    redact_is("", "");
    CHECK(kt_mask_redact(NULL) == NULL);

    KT_TEST_DONE();
}
