/* SPDX-License-Identifier: MIT
 * Author: Peter Kokosharov <peter.kokosharov@gmail.com>
 * String builder, dialog model and in-memory icon rendering.
 */
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include "kt_icon.h"
#include "kt_test.h"
#include "kt_util.h"
#include "kt_view.h"

static void test_sb(void)
{
    kt_sb sb;
    char *s;
    int i;
    kt_sb_init(&sb);
    s = kt_sb_take(&sb);
    CHECK_STR(s, "");
    free(s);
    kt_sb_append(&sb, "ab");
    kt_sb_printf(&sb, "-%d-", 42);
    for (i = 0; i < 500; i++) kt_sb_append(&sb, "x");
    CHECK_INT(sb.len, 2 + 4 + 500);
    kt_sb_printf(&sb, "%s", "0123456789012345678901234567890123456789012345678901234567890123456789"
                 "0123456789012345678901234567890123456789012345678901234567890123456789"
                 "0123456789012345678901234567890123456789012345678901234567890123456789");
    CHECK_INT(sb.len, 506 + 210);
    s = kt_sb_take(&sb);
    CHECK(strncmp(s, "ab-42-x", 7) == 0);
    free(s);
    CHECK(kt_strncasecmp("Bearer", "bEARER", 6) == 0);
    CHECK(kt_strncasecmp("abc", "abd", 3) != 0);
}

static void test_dialog(void)
{
    kt_dialog *d = kt_dialog_new("models", "Models");
    const char *row1[] = { "a", "b" };
    const char *row2[] = { "c", NULL };
    int i;
    CHECK(d != NULL);
    CHECK_INT(kt_dialog_add_row(d, row1), -1);          /* no columns yet */
    CHECK_INT(kt_dialog_add_column(d, "Name"), 0);
    CHECK_INT(kt_dialog_add_column(d, "Class"), 0);
    for (i = 0; i < 40; i++) CHECK_INT(kt_dialog_add_row(d, i % 2 ? row2 : row1), 0);
    CHECK_INT(kt_dialog_add_column(d, "Late"), -1);     /* columns only before rows */
    CHECK_INT(d->nrows, 40);
    CHECK_STR(d->cells[3], "");                          /* NULL cell became empty */
    CHECK_STR(d->cells[2], "c");
    kt_dialog_set_summary(d, "hi");
    kt_dialog_set_summary(d, "again");
    CHECK_STR(d->summary, "again");
    CHECK_INT(kt_dialog_add_button(d, "refresh", "Refresh"), 0);
    CHECK_STR(d->buttons[0].id, "refresh");
    kt_dialog_free(d);
}

static void test_icon(void)
{
    enum { N = 32 };
    uint8_t px[N * N * 4];
    const kt_state states[4] = { KT_STATE_OK, KT_STATE_DEGRADED, KT_STATE_DOWN, KT_STATE_STARTING };
    int s;
    for (s = 0; s < 4; s++) {
        uint32_t rgb = kt_icon_state_rgb(states[s]);
        uint8_t r = (uint8_t)(rgb >> 16), g = (uint8_t)(rgb >> 8), b = (uint8_t)rgb;
        const uint8_t *p;
        kt_icon_render(N, states[s], 1, px);
        CHECK_INT(px[3], 0);                                   /* corner is transparent */
        p = px + (16 * N + 25) * 4;                             /* inside the disc, away from the K */
        CHECK_INT(p[3], 255);
        CHECK_INT(p[2], r);                                     /* BGRA: byte 2 = red */
        CHECK_INT(p[1], g);
        CHECK_INT(p[0], b);
        p = px + (16 * N + 11) * 4;                             /* on the stem of the K: white */
        CHECK(p[0] > 240 && p[1] > 240 && p[2] > 240);
        kt_icon_render(N, states[s], 0, px);                   /* RGBA: byte 0 = red */
        CHECK_INT(px[(16 * N + 25) * 4 + 0], r);
    }
    CHECK(kt_icon_state_rgb(KT_STATE_OK) != kt_icon_state_rgb(KT_STATE_DOWN));
    kt_icon_render(16, KT_STATE_OK, 1, px);                    /* smallest tray size works too */
    CHECK(px[(8 * 16 + 8) * 4 + 3] == 255);
}

int main(void)
{
    test_sb();
    test_dialog();
    test_icon();
    KT_TEST_DONE();
}
