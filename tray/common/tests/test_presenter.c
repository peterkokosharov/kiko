/* SPDX-License-Identifier: MIT
 * Author: Peter Kokosharov <peter.kokosharov@gmail.com>
 * Presenter tests with a fake view and a fake HTTP backend: no GUI, no sockets, any OS.
 */
#include "fake_view.h"
#include "kt_test.h"

static fake_view V;
static kt_view   view;
static kt_presenter *P;

static int  g_svc_rc;
static char g_svc_msg[64];
static int  g_svc_calls;
static kt_action g_svc_last;

static int host_service(void *ctx, kt_action a, char *msg, size_t sz)
{
    (void)ctx;
    g_svc_calls++;
    g_svc_last = a;
    snprintf(msg, sz, "%s", g_svc_msg);
    return g_svc_rc;
}

static void settle(void)
{
    int i;
    for (i = 0; i < 4; i++) {
        kt_presenter_wait_idle(P, 3000);
        sleep_ms(10);
        fv_pump(&V);
    }
}

#define WAIT(cond) \
    do { int t_; for (t_ = 0; t_ < 400 && !(cond); t_++) { \
             kt_presenter_wait_idle(P, 50); fv_pump(&V); if (cond) break; sleep_ms(5); } } while (0)

static void setup(const char *health_body, int health_rc, const char *keystore_label)
{
    kt_config cfg;
    kt_host host;
    fake_reset();
    fake_set("/health", health_rc, 200, health_body);
    fv_init(&V, &view, keystore_label);
    memset(&cfg, 0, sizeof cfg);
    cfg.port = 4000;
    cfg.api_token = FAKE_TOKEN;
    cfg.token_source = "env KIKO_API_TOKEN";
    cfg.command_text = "kiko-lite serve";
    cfg.version = "9.9";
    cfg.poll_interval_ms = 3600000;   /* only the first poll runs; tests trigger more explicitly */
    cfg.start_grace_ms = 400;
    cfg.http = fake_http;
    host.ctx = NULL;
    host.service = host_service;
    g_svc_rc = 0;
    g_svc_calls = 0;
    snprintf(g_svc_msg, sizeof g_svc_msg, "kiko started");
    P = kt_presenter_new(&view, &host, &cfg);
    CHECK(P != NULL);
    CHECK_INT(kt_presenter_start(P), 0);
}

static void teardown(void)
{
    kt_presenter_stop(P);
    kt_presenter_wait_idle(P, 3000);
    fv_pump(&V);
    kt_presenter_free(P);
    P = NULL;
}

static int menu_index(const char *id)
{
    int i;
    for (i = 0; i < V.menu_n; i++)
        if (strcmp(V.menu_ids[i], id) == 0) return i;
    return -1;
}

static int menu_label_index(const char *label)
{
    int i;
    for (i = 0; i < V.menu_n; i++)
        if (strcmp(V.menu_labels[i], label) == 0) return i;
    return -1;
}

static const captured_dialog *last_dialog(const char *kind)
{
    int i;
    for (i = V.ndlg - 1; i >= 0; i--)
        if (strcmp(V.dlg[i].kind, kind) == 0) return &V.dlg[i];
    return NULL;
}

static int note_contains(const char *s)
{
    int i;
    for (i = 0; i < V.nnotes; i++)
        if (strstr(V.notes[i], s)) return 1;
    return 0;
}

static void open_and_wait(const char *id)
{
    int before = V.ndlg;
    V.events->on_menu_item(V.events->ctx, id);
    WAIT(V.ndlg > before);
}

static int token_leaked(void)
{
    int i;
    for (i = 0; i < V.ndlg; i++)
        if (strstr(V.dlg[i].summary, "SECRETTOKEN") || strstr(V.dlg[i].text, "SECRETTOKEN")
            || strstr(V.dlg[i].cells, "SECRETTOKEN"))
            return 1;
    for (i = 0; i < V.nnotes; i++)
        if (strstr(V.notes[i], "SECRETTOKEN")) return 1;
    return strstr(V.tooltip, "SECRETTOKEN") != NULL;
}

static void test_states_and_menu(void)
{
    int i;
    setup("{\"status\":\"ok\"}", 0, NULL);
    WAIT(V.state == KT_STATE_OK && kt_presenter_state(P) == KT_STATE_OK);
    CHECK_INT(V.state, KT_STATE_OK);
    CHECK(strstr(V.tooltip, "running") != NULL);
    CHECK(V.bound);

    i = menu_index(KT_ID_START);   CHECK(i >= 0); CHECK_INT(V.menu_enabled[i], 0);   /* running: no start */
    i = menu_index(KT_ID_STOP);    CHECK(i >= 0); CHECK_INT(V.menu_enabled[i], 1);
    i = menu_index(KT_ID_RESTART); CHECK(i >= 0); CHECK_INT(V.menu_enabled[i], 1);
    i = menu_label_index("Codex (coming)"); CHECK(i >= 0); CHECK_INT(V.menu_enabled[i], 0);
    CHECK(menu_index(KT_ID_STATUS) >= 0);
    CHECK(menu_index(KT_ID_LOGS) >= 0);
    CHECK(menu_index(KT_ID_MODELS) >= 0);
    CHECK(menu_index(KT_ID_CLASSES) >= 0);
    CHECK(menu_index(KT_ID_KEYS) >= 0);
    CHECK(menu_index(KT_ID_COSTS) >= 0);
    CHECK(menu_index(KT_ID_SETTINGS) >= 0);
    CHECK(menu_index(KT_ID_ABOUT) >= 0);
    CHECK(menu_index(KT_ID_QUIT) >= 0);
    CHECK(menu_label_index("kiko: running") == 0);

    /* kiko goes away: DOWN, start enabled, a notification */
    fake_set("/health", KT_HTTP_ERR_CONNECT, 0, "");
    kt_presenter_poll_now(P);
    WAIT(V.state == KT_STATE_DOWN);
    CHECK_INT(V.state, KT_STATE_DOWN);
    i = menu_index(KT_ID_START); CHECK_INT(V.menu_enabled[i], 1);
    i = menu_index(KT_ID_STOP);  CHECK_INT(V.menu_enabled[i], 0);
    CHECK(note_contains("stopped responding"));

    /* answers but not ok: DEGRADED (kiko-full says "healthy", kiko-lite says "ok") */
    fake_set("/health", 0, 200, "{\"status\":\"loading\"}");
    kt_presenter_poll_now(P);
    WAIT(V.state == KT_STATE_DEGRADED);
    CHECK_INT(V.state, KT_STATE_DEGRADED);
    fake_set("/health", 0, 200, "{\"status\":\"healthy\",\"classes\":{}}");
    kt_presenter_poll_now(P);
    WAIT(V.state == KT_STATE_OK);
    CHECK_INT(V.state, KT_STATE_OK);
    fake_set("/health", 0, 500, "not json at all");
    kt_presenter_poll_now(P);
    WAIT(V.state == KT_STATE_DEGRADED);
    CHECK_INT(V.state, KT_STATE_DEGRADED);

    settle();
    CHECK_INT(g_http_on_ui_thread, 0);   /* the UI thread never made an HTTP call */
    CHECK_INT(g_health_got_token, 0);    /* the token never goes to /health */
    teardown();
}

static void test_start_stop(void)
{
    int i;
    setup("{\"status\":\"ok\"}", KT_HTTP_ERR_CONNECT, NULL);
    WAIT(V.state == KT_STATE_DOWN);
    CHECK_INT(V.state, KT_STATE_DOWN);

    V.events->on_menu_item(V.events->ctx, KT_ID_START);
    CHECK_INT(V.state, KT_STATE_STARTING);                 /* immediate, before the worker ran */
    i = menu_index(KT_ID_START); CHECK_INT(V.menu_enabled[i], 0);   /* busy: all actions off */
    i = menu_index(KT_ID_RESTART); CHECK_INT(V.menu_enabled[i], 0);
    WAIT(note_contains("kiko started"));
    CHECK(note_contains("kiko started"));
    CHECK_INT(g_svc_calls, 1);
    CHECK_INT(g_svc_last, KT_ACT_START);
    settle();
    CHECK_INT(V.state, KT_STATE_STARTING);                 /* still refused, but inside the grace time */

    fake_set("/health", 0, 200, "{\"status\":\"ok\"}");
    kt_presenter_poll_now(P);
    WAIT(V.state == KT_STATE_OK);
    CHECK_INT(V.state, KT_STATE_OK);

    /* a failing stop reports the reason and does not hang the menu */
    g_svc_rc = -1;
    snprintf(g_svc_msg, sizeof g_svc_msg, "boom");
    V.events->on_menu_item(V.events->ctx, KT_ID_STOP);
    WAIT(note_contains("boom"));
    CHECK(note_contains("boom"));
    CHECK_INT(g_svc_last, KT_ACT_STOP);
    settle();
    i = menu_index(KT_ID_RESTART); CHECK_INT(V.menu_enabled[i], 1);

    /* after the grace time a dead kiko shows DOWN again */
    fake_set("/health", KT_HTTP_ERR_CONNECT, 0, "");
    g_svc_rc = 0;
    snprintf(g_svc_msg, sizeof g_svc_msg, "restarted");
    V.events->on_menu_item(V.events->ctx, KT_ID_RESTART);
    WAIT(note_contains("restarted"));
    CHECK_INT(g_svc_last, KT_ACT_RESTART);
    sleep_ms(500);                                          /* grace is 400 ms in this test */
    kt_presenter_poll_now(P);
    WAIT(V.state == KT_STATE_DOWN);
    CHECK_INT(V.state, KT_STATE_DOWN);
    teardown();
}

static void test_quit_and_default_action(void)
{
    setup("{\"status\":\"ok\"}", 0, NULL);
    WAIT(V.state == KT_STATE_OK);
    V.events->on_default_action(V.events->ctx);              /* double click opens Status */
    WAIT(last_dialog("status") != NULL);
    CHECK(last_dialog("status") != NULL);
    V.events->on_menu_item(V.events->ctx, "codex");          /* disabled item id: ignored */
    V.events->on_menu_item(V.events->ctx, "");
    V.events->on_menu_item(V.events->ctx, KT_ID_QUIT);
    CHECK_INT(V.quit_called, 1);
    CHECK_INT(g_svc_calls, 0);                               /* quitting does not stop kiko */
    teardown();
}

static void test_dialogs(void)
{
    const captured_dialog *d;

    setup("{\"status\":\"ok\"}", 0, "Open Credential Manager");
    WAIT(V.state == KT_STATE_OK);

    /* no management API yet: every API dialog says so, nothing crashes */
    open_and_wait(KT_ID_MODELS);
    d = last_dialog("models");
    CHECK(d != NULL);
    CHECK(strstr(d->summary, "not available") != NULL);
    CHECK_INT(d->nrows, 0);
    open_and_wait(KT_ID_STATUS);
    d = last_dialog("status");
    CHECK(strstr(d->text, "Health: HTTP 200") != NULL);
    CHECK(strstr(d->text, "Only the /health state is shown") != NULL);

    /* models and classes */
    fake_set("/kiko/v1/models", 0, 200,
             "{\"models\":[{\"name\":\"opus\",\"class\":\"OPUS\",\"enabled\":true,\"status\":\"ok\"},"
             "{\"name\":\"haiku\",\"class\":\"HAIKU\",\"enabled\":false,\"status\":\"unknown\"}]}");
    open_and_wait(KT_ID_MODELS);
    d = last_dialog("models");
    CHECK_INT(d->nrows, 2);
    CHECK_INT(d->ncols, 4);
    CHECK(strstr(d->cells, "opus|OPUS|yes|ok\n") != NULL);
    CHECK(strstr(d->cells, "haiku|HAIKU|no|unknown\n") != NULL);
    fake_set("/kiko/v1/classes", 0, 200,
             "{\"classes\":[{\"name\":\"OPUS\",\"keyword\":\"opus\",\"role\":\"architect\"}]}");
    open_and_wait(KT_ID_CLASSES);
    CHECK(strstr(last_dialog("classes")->cells, "OPUS|opus|architect") != NULL);

    /* keys: names and present flags only; a value smuggled into the reply is never shown */
    fake_set("/kiko/v1/keys", 0, 200,
             "{\"keys\":[{\"name\":\"NVIDIA_API_KEY\",\"present\":true,\"value\":\"sk-LEAKEDVALUE123456\"},"
             "{\"name\":\"OPENAI_API_KEY\",\"present\":false},"
             "{\"name\":\"sk-AnotherLeak987654321\",\"present\":true}]}");
    open_and_wait(KT_ID_KEYS);
    d = last_dialog("keys");
    CHECK_INT(d->nrows, 3);
    CHECK(strstr(d->cells, "NVIDIA_API_KEY: *****|present\n") != NULL);
    CHECK(strstr(d->cells, "OPENAI_API_KEY: *****|missing\n") != NULL);
    CHECK(strstr(d->cells, "LEAKED") == NULL);
    CHECK(strstr(d->cells, "AnotherLeak") == NULL);
    CHECK(strstr(d->buttons, "open_keystore") != NULL);
    V.events->on_dialog_button(V.events->ctx, "keys", "open_keystore");
    CHECK_INT(V.keystore_opened, 1);

    /* costs and logs (logs are redacted a second time on our side) */
    fake_set("/kiko/v1/costs?days=30", 0, 200,
             "{\"days\":30,\"today_usd\":0.5,\"period_usd\":12.25,\"cap_usd\":50,"
             "\"per_model\":[{\"model\":\"sonnet\",\"usd\":12.25,\"tokens_in\":1000,\"tokens_out\":500}]}");
    open_and_wait(KT_ID_COSTS);
    d = last_dialog("costs");
    CHECK(strstr(d->summary, "12.2500") != NULL);
    CHECK(strstr(d->summary, "cap $50.00") != NULL);
    CHECK(strstr(d->cells, "sonnet|12.2500|1000|500") != NULL);
    fake_set("/kiko/v1/logs?lines=200", 0, 200,
             "{\"lines\":[\"started\",\"Authorization: Bearer abcdef.123456\"]}");
    open_and_wait(KT_ID_LOGS);
    d = last_dialog("logs");
    CHECK(strstr(d->text, "started\n") != NULL);
    CHECK(strstr(d->text, "abcdef") == NULL);
    CHECK(strstr(d->text, "[REDACTED]") != NULL);

    /* local dialogs show no token, only where it comes from */
    open_and_wait(KT_ID_SETTINGS);
    d = last_dialog("settings");
    CHECK(strstr(d->text, "env KIKO_API_TOKEN") != NULL);
    open_and_wait(KT_ID_ABOUT);
    CHECK(strstr(last_dialog("about")->text, "9.9") != NULL);

    /* error cases */
    fake_set("/kiko/v1/models", 0, 401, "{\"error\":{\"code\":\"unauthorized\",\"message\":\"bad token\"}}");
    open_and_wait(KT_ID_MODELS);
    CHECK(strstr(last_dialog("models")->summary, "refused the token") != NULL);
    fake_set("/kiko/v1/models", 0, 500, "{\"error\":{\"code\":\"internal\",\"message\":\"db locked\"}}");
    open_and_wait(KT_ID_MODELS);
    CHECK(strstr(last_dialog("models")->summary, "db locked") != NULL);
    fake_set("/kiko/v1/models", KT_HTTP_ERR_CONNECT, 0, "");
    open_and_wait(KT_ID_MODELS);
    CHECK(strstr(last_dialog("models")->summary, "not running") != NULL);
    fake_set("/kiko/v1/models", 0, 200, "<html>proxy page</html>");
    open_and_wait(KT_ID_MODELS);
    CHECK(strstr(last_dialog("models")->summary, "error") != NULL);

    /* the Refresh button rebuilds the same dialog */
    {
        int before = V.ndlg;
        V.events->on_dialog_button(V.events->ctx, "costs", "refresh");
        WAIT(V.ndlg > before);
        CHECK_STR(V.dlg[V.ndlg - 1].kind, "costs");
    }

    CHECK_INT(g_api_missing_token, 0);   /* every /kiko/ call carried the token */
    CHECK_INT(token_leaked(), 0);        /* and the token never reached the UI */
    CHECK_INT(g_http_on_ui_thread, 0);
    teardown();
}

static void test_set_class_hook(void)
{
    setup("{\"status\":\"ok\"}", 0, NULL);
    WAIT(V.state == KT_STATE_OK);
    CHECK_INT(kt_presenter_set_model_class(P, "a/b", "HAIKU"), -1);          /* path injection */
    CHECK_INT(kt_presenter_set_model_class(P, "opus", "haiku\"}"), -1);      /* JSON injection */
    CHECK_INT(kt_presenter_set_model_class(P, "", "HAIKU"), -1);
    CHECK_INT(kt_presenter_set_model_class(P, "..", "HAIKU"), -1);
    CHECK_INT(kt_presenter_set_model_class(P, "opus", "Haiku-1"), -1);   /* class is [A-Z0-9_]+ */
    fake_set("/kiko/v1/models/opus/class", 0, 200, "{\"name\":\"opus\",\"class\":\"HAIKU\"}");
    CHECK_INT(kt_presenter_set_model_class(P, "opus", "HAIKU"), 0);
    WAIT(note_contains("now class HAIKU"));
    CHECK(note_contains("Model opus is now class HAIKU"));
    CHECK_STR(g_last_post_path, "/kiko/v1/models/opus/class");
    CHECK_STR(g_last_post_body, "{\"class\":\"HAIKU\"}");
    fake_set("/kiko/v1/models/opus/class", 0, 404, "{\"error\":{\"message\":\"x\"}}");
    CHECK_INT(kt_presenter_set_model_class(P, "opus", "HAIKU"), 0);
    WAIT(note_contains("not available"));
    CHECK(note_contains("not available"));
    teardown();
}

int main(void)
{
    kt_mutex_init(&g_http_mu);
    test_states_and_menu();
    test_start_stop();
    test_quit_and_default_action();
    test_dialogs();
    test_set_class_hook();
    KT_TEST_DONE();
}
