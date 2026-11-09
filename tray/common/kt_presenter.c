/* SPDX-License-Identifier: MIT
 * Author: Peter Kokosharov <peter.kokosharov@gmail.com>
 */
#include "kt_presenter.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "kt_json.h"
#include "kt_mask.h"
#include "kt_thread.h"
#include "kt_util.h"

#define KT_HTTP_TIMEOUT_MS 3000

struct kt_presenter {
    kt_view    view;
    kt_host    host;
    kt_events  events;

    int        port;
    char      *token;
    char      *token_source;
    char      *command_text;
    char      *version;
    int        poll_interval_ms;
    int        start_grace_ms;
    kt_http_fn http;

    kt_worker *worker;
    kt_thread  poll_th;
    int        poll_started;
    kt_mutex   mu;          /* guards stop and starting_until */
    kt_cond    poll_wake;
    int        stop;
    uint64_t   starting_until;   /* kt_now_ms deadline; 0 = none */

    /* UI-thread-only state */
    kt_state   state;
    int        have_state;
    int        busy;
    char       tooltip[160];
};

/* ---------------------------------------------------------------- small helpers */

static void post(kt_presenter *p, kt_ui_fn fn, void *arg) { p->view.post_to_ui(p->view.ctx, fn, arg); }

static void set_starting_until(kt_presenter *p, uint64_t t)
{
    kt_mutex_lock(&p->mu);
    p->starting_until = t;
    kt_mutex_unlock(&p->mu);
}

static const char *state_word(kt_state s)
{
    switch (s) {
    case KT_STATE_OK:       return "running";
    case KT_STATE_DEGRADED: return "degraded";
    case KT_STATE_DOWN:     return "not running";
    default:                return "starting";
    }
}

/* ---------------------------------------------------------------- UI thread: menu and state */

static void rebuild_menu(kt_presenter *p)
{
    kt_menu_item m[20];
    char status[64];
    int n = 0;
    int down = p->state == KT_STATE_DOWN;
    int idle = !p->busy;

    snprintf(status, sizeof status, "kiko: %s", state_word(p->state));
#define ITEM(i, l, e) do { m[n].id = (i); m[n].label = (l); m[n].enabled = (e); m[n].separator = 0; n++; } while (0)
#define SEP() do { m[n].id = ""; m[n].label = ""; m[n].enabled = 0; m[n].separator = 1; n++; } while (0)
    ITEM("", status, 0);
    SEP();
    ITEM(KT_ID_START, "Start kiko", idle && down);
    ITEM(KT_ID_STOP, "Stop kiko", idle && !down);
    ITEM(KT_ID_RESTART, "Restart kiko", idle);
    SEP();
    ITEM(KT_ID_STATUS, "Status", 1);
    ITEM(KT_ID_LOGS, "Logs", 1);
    ITEM(KT_ID_MODELS, "Models", 1);
    ITEM(KT_ID_CLASSES, "Model classes", 1);
    ITEM(KT_ID_KEYS, "Keys", 1);
    ITEM(KT_ID_COSTS, "Costs", 1);
    ITEM(KT_ID_SETTINGS, "Settings", 1);
    ITEM(KT_ID_ABOUT, "About", 1);
    SEP();
    ITEM("codex", "Codex (coming)", 0);
    SEP();
    ITEM(KT_ID_QUIT, "Quit", 1);
#undef ITEM
#undef SEP
    p->view.set_menu(p->view.ctx, m, n);
}

typedef struct { kt_presenter *p; kt_state s; char text[160]; } ui_state;

static void ui_apply_state(void *arg)
{
    ui_state *u = (ui_state *)arg;
    kt_presenter *p = u->p;
    kt_state prev = p->state;
    int had = p->have_state;

    p->state = u->s;
    p->have_state = 1;
    if (!had || u->s != prev || strcmp(p->tooltip, u->text) != 0) {
        snprintf(p->tooltip, sizeof p->tooltip, "%s", u->text);
        p->view.set_state(p->view.ctx, u->s, p->tooltip);
    }
    if (!had || u->s != prev) rebuild_menu(p);
    if (had && (prev == KT_STATE_OK || prev == KT_STATE_DEGRADED) && u->s == KT_STATE_DOWN && !p->busy)
        p->view.notify(p->view.ctx, "kiko stopped responding");
    free(u);
}

typedef struct { kt_presenter *p; char text[256]; } ui_text;

static void ui_notify(void *arg)
{
    ui_text *u = (ui_text *)arg;
    u->p->view.notify(u->p->view.ctx, u->text);
    free(u);
}

static void post_notify(kt_presenter *p, const char *text)
{
    ui_text *u = (ui_text *)calloc(1, sizeof *u);
    if (!u) return;
    u->p = p;
    snprintf(u->text, sizeof u->text, "%s", text);
    post(p, ui_notify, u);
}

/* ---------------------------------------------------------------- worker: polling */

static int http_call(kt_presenter *p, const char *method, const char *path, const char *body,
                     kt_http_resp *r)
{
    /* The token goes only to the management API, never to /health. */
    const char *bearer = (p->token && strncmp(path, "/kiko/", 6) == 0) ? p->token : NULL;
    return p->http(method, p->port, path, bearer, body, KT_HTTP_TIMEOUT_MS, r);
}

static void poll_and_post(kt_presenter *p)
{
    kt_http_resp r;
    ui_state *u = (ui_state *)calloc(1, sizeof *u);
    int rc;
    if (!u) return;
    u->p = p;
    memset(&r, 0, sizeof r);
    rc = http_call(p, "GET", "/health", NULL, &r);
    if (rc != 0) {
        uint64_t until;
        kt_mutex_lock(&p->mu);
        until = p->starting_until;
        kt_mutex_unlock(&p->mu);
        if (until != 0 && kt_now_ms() < until) {
            u->s = KT_STATE_STARTING;
            snprintf(u->text, sizeof u->text, "KIKO: starting...");
        } else {
            u->s = KT_STATE_DOWN;
            snprintf(u->text, sizeof u->text, "KIKO: not running (port %d)", p->port);
        }
    } else {
        kt_json *j = kt_json_parse(r.body, r.body_len);
        const char *st = kt_json_string(kt_json_get(j, "status"), "");
        if (r.status == 200 && (strcmp(st, "ok") == 0 || strcmp(st, "healthy") == 0)) {
            u->s = KT_STATE_OK;
            snprintf(u->text, sizeof u->text, "KIKO: running (port %d)", p->port);
            set_starting_until(p, 0);
        } else {
            u->s = KT_STATE_DEGRADED;
            snprintf(u->text, sizeof u->text, "KIKO: degraded (HTTP %d)", r.status);
        }
        kt_json_free(j);
    }
    kt_http_resp_free(&r);
    post(p, ui_apply_state, u);
}

static void poll_job(void *arg) { poll_and_post((kt_presenter *)arg); }

void kt_presenter_poll_now(kt_presenter *p) { kt_worker_submit(p->worker, poll_job, NULL, p); }

static void poll_thread_main(void *arg)
{
    kt_presenter *p = (kt_presenter *)arg;
    kt_mutex_lock(&p->mu);
    while (!p->stop) {
        kt_mutex_unlock(&p->mu);
        poll_and_post(p);
        kt_mutex_lock(&p->mu);
        if (p->stop) break;
        kt_cond_wait_ms(&p->poll_wake, &p->mu, (unsigned)p->poll_interval_ms);
    }
    kt_mutex_unlock(&p->mu);
}

/* ---------------------------------------------------------------- actions: start/stop/restart */

typedef struct { kt_presenter *p; kt_action a; } action_job;
typedef struct { kt_presenter *p; int ok; char msg[256]; } ui_done;

static void ui_action_done(void *arg)
{
    ui_done *u = (ui_done *)arg;
    kt_presenter *p = u->p;
    p->busy = 0;
    p->view.notify(p->view.ctx, u->msg);
    rebuild_menu(p);
    kt_presenter_poll_now(p);
    free(u);
}

static void action_run(void *arg)
{
    action_job *j = (action_job *)arg;
    kt_presenter *p = j->p;
    ui_done *u = (ui_done *)calloc(1, sizeof *u);
    if (u) {
        u->p = p;
        if (!p->host.service) {
            snprintf(u->msg, sizeof u->msg, "Service control is not available on this platform");
        } else {
            u->ok = p->host.service(p->host.ctx, j->a, u->msg, sizeof u->msg) == 0;
        }
        if (u->msg[0] == '\0') snprintf(u->msg, sizeof u->msg, u->ok ? "Done" : "The action failed");
        if (u->ok && j->a != KT_ACT_STOP)
            set_starting_until(p, kt_now_ms() + (uint64_t)p->start_grace_ms);
        else
            set_starting_until(p, 0);
        post(p, ui_action_done, u);
    }
    free(j);
}

static void action_drop(void *arg) { free(arg); }

static void start_action(kt_presenter *p, kt_action a)
{
    action_job *j;
    if (p->busy) return;
    j = (action_job *)calloc(1, sizeof *j);
    if (!j) return;
    j->p = p;
    j->a = a;
    p->busy = 1;
    if (a != KT_ACT_STOP) {
        set_starting_until(p, kt_now_ms() + (uint64_t)p->start_grace_ms);
        p->state = KT_STATE_STARTING;
        snprintf(p->tooltip, sizeof p->tooltip, "KIKO: starting...");
        p->view.set_state(p->view.ctx, p->state, p->tooltip);
    }
    rebuild_menu(p);
    if (kt_worker_submit(p->worker, action_run, action_drop, j) != 0) {
        free(j);
        p->busy = 0;
        rebuild_menu(p);
    }
}

/* ---------------------------------------------------------------- worker: management API */

typedef enum { API_OK, API_DOWN, API_UNAVAILABLE, API_UNAUTH, API_ERROR } api_rc;

/* GET path; on API_OK *out is the parsed JSON root (caller frees).  err gets a short reason. */
static api_rc api_call(kt_presenter *p, const char *method, const char *path, const char *body,
                       kt_json **out, char *err, size_t errsz)
{
    kt_http_resp r;
    kt_json *j;
    int rc;

    *out = NULL;
    err[0] = '\0';
    memset(&r, 0, sizeof r);
    rc = http_call(p, method, path, body, &r);
    if (rc == KT_HTTP_ERR_CONNECT) return API_DOWN;
    if (rc != 0) {
        snprintf(err, errsz, "network error %d", rc);
        return API_ERROR;
    }
    j = kt_json_parse(r.body, r.body_len);
    if (r.status == 200 && j) {
        kt_http_resp_free(&r);
        *out = j;
        return API_OK;
    }
    snprintf(err, errsz, "%s",
             kt_json_string(kt_json_get(kt_json_get(j, "error"), "message"), ""));
    kt_json_free(j);
    rc = r.status;
    kt_http_resp_free(&r);
    if (rc == 404 || rc == 405) return API_UNAVAILABLE;
    if (rc == 401 || rc == 403) return API_UNAUTH;
    if (err[0] == '\0') snprintf(err, errsz, "HTTP %d", rc);
    return API_ERROR;
}

static void problem_text(kt_presenter *p, api_rc rc, const char *err, kt_sb *sb)
{
    switch (rc) {
    case API_DOWN:
        kt_sb_printf(sb, "kiko is not running (nothing answers on 127.0.0.1:%d).", p->port);
        break;
    case API_UNAVAILABLE:
        kt_sb_append(sb, "Management API not available: this kiko version has no /kiko/v1 endpoints yet.");
        break;
    case API_UNAUTH:
        kt_sb_printf(sb, "The management API refused the token (token source: %s).", p->token_source);
        break;
    default:
        kt_sb_printf(sb, "Management API error: %s", err[0] ? err : "unknown");
        break;
    }
}

/* ---------------------------------------------------------------- worker: dialog builders */

static kt_dialog *new_dialog(const char *kind, const char *title)
{
    kt_dialog *d = kt_dialog_new(kind, title);
    if (d) kt_dialog_add_button(d, "refresh", "Refresh");
    return d;
}

static kt_dialog *problem_dialog(kt_presenter *p, const char *kind, const char *title, api_rc rc,
                                 const char *err)
{
    kt_sb sb;
    char *s;
    kt_dialog *d = new_dialog(kind, title);
    if (!d) return NULL;
    kt_sb_init(&sb);
    problem_text(p, rc, err, &sb);
    s = kt_sb_take(&sb);
    kt_dialog_set_summary(d, s);
    free(s);
    return d;
}

static kt_dialog *build_table(kt_presenter *p, const char *kind, const char *title, const char *path,
                              const char *array_key, const char *const *cols, const char *const *fields,
                              int ncols)
{
    kt_json *j;
    char err[160];
    api_rc rc = api_call(p, "GET", path, NULL, &j, err, sizeof err);
    kt_dialog *d;
    const kt_json *arr;
    size_t i;
    int c;
    char summary[64];

    if (rc != API_OK) return problem_dialog(p, kind, title, rc, err);
    d = new_dialog(kind, title);
    if (!d) { kt_json_free(j); return NULL; }
    for (c = 0; c < ncols; c++) kt_dialog_add_column(d, cols[c]);
    arr = kt_json_get(j, array_key);
    for (i = 0; i < kt_json_count(arr); i++) {
        const kt_json *it = kt_json_at(arr, i);
        const char *cells[6] = {0};
        char flag[6][32];
        for (c = 0; c < ncols && c < 6; c++) {
            const kt_json *f = kt_json_get(it, fields[c]);
            if (kt_json_is_bool(f)) {
                snprintf(flag[c], sizeof flag[c], "%s", kt_json_bool(f, 0) ? "yes" : "no");
                cells[c] = flag[c];
            } else if (kt_json_is_number(f)) {
                snprintf(flag[c], sizeof flag[c], "%.0f", kt_json_number(f, 0.0));
                cells[c] = flag[c];
            } else {
                cells[c] = kt_json_string(f, "");
            }
        }
        kt_dialog_add_row(d, cells);
    }
    snprintf(summary, sizeof summary, "%d entries", d->nrows);
    kt_dialog_set_summary(d, summary);
    kt_json_free(j);
    return d;
}

static kt_dialog *build_models(kt_presenter *p)
{
    static const char *const cols[] = { "Name", "Class", "Enabled", "Status" };
    static const char *const fields[] = { "name", "class", "enabled", "status" };
    return build_table(p, "models", "kiko - Models", "/kiko/v1/models", "models", cols, fields, 4);
}

static kt_dialog *build_classes(kt_presenter *p)
{
    static const char *const cols[] = { "Class", "Keyword", "Role" };
    static const char *const fields[] = { "name", "keyword", "role" };
    kt_dialog *d = build_table(p, "classes", "kiko - Model classes", "/kiko/v1/classes", "classes",
                               cols, fields, 3);
    if (d && d->ncols > 0)
        kt_dialog_set_footer(d, "Read-only here. A class is changed with POST /kiko/v1/models/{name}/class.");
    return d;
}

static kt_dialog *build_keys(kt_presenter *p)
{
    kt_json *j;
    char err[160];
    api_rc rc = api_call(p, "GET", "/kiko/v1/keys", NULL, &j, err, sizeof err);
    kt_dialog *d;
    const kt_json *arr;
    size_t i;

    if (rc != API_OK) {
        d = problem_dialog(p, "keys", "kiko - Keys", rc, err);
    } else {
        d = new_dialog("keys", "kiko - Keys");
        if (!d) { kt_json_free(j); return NULL; }
        kt_dialog_add_column(d, "Key");
        kt_dialog_add_column(d, "Status");
        arr = kt_json_get(j, "keys");
        /* Only the NAME and the present flag are read.  No other field is ever looked at. */
        for (i = 0; i < kt_json_count(arr); i++) {
            const kt_json *it = kt_json_at(arr, i);
            char label[96];
            const char *cells[2];
            kt_mask_key_label(label, sizeof label, kt_json_string(kt_json_get(it, "name"), ""));
            cells[0] = label;
            cells[1] = kt_mask_status_word(kt_json_bool(kt_json_get(it, "present"), 0));
            kt_dialog_add_row(d, cells);
        }
        kt_dialog_set_summary(d, "Key values are never fetched or shown; only whether each key exists.");
        kt_json_free(j);
    }
    if (d && p->view.key_store_label) kt_dialog_add_button(d, "open_keystore", p->view.key_store_label);
    return d;
}

static kt_dialog *build_costs(kt_presenter *p)
{
    kt_json *j;
    char err[160], summary[200];
    api_rc rc = api_call(p, "GET", "/kiko/v1/costs?days=30", NULL, &j, err, sizeof err);
    kt_dialog *d;
    const kt_json *arr, *cap;
    size_t i;

    if (rc != API_OK) return problem_dialog(p, "costs", "kiko - Costs", rc, err);
    d = new_dialog("costs", "kiko - Costs");
    if (!d) { kt_json_free(j); return NULL; }
    kt_dialog_add_column(d, "Model");
    kt_dialog_add_column(d, "USD");
    kt_dialog_add_column(d, "Tokens in");
    kt_dialog_add_column(d, "Tokens out");
    arr = kt_json_get(j, "per_model");
    for (i = 0; i < kt_json_count(arr); i++) {
        const kt_json *it = kt_json_at(arr, i);
        char usd[32], tin[32], tout[32];
        const char *cells[4];
        snprintf(usd, sizeof usd, "%.4f", kt_json_number(kt_json_get(it, "usd"), 0.0));
        snprintf(tin, sizeof tin, "%.0f", kt_json_number(kt_json_get(it, "tokens_in"), 0.0));
        snprintf(tout, sizeof tout, "%.0f", kt_json_number(kt_json_get(it, "tokens_out"), 0.0));
        cells[0] = kt_json_string(kt_json_get(it, "model"), "?");
        cells[1] = usd;
        cells[2] = tin;
        cells[3] = tout;
        kt_dialog_add_row(d, cells);
    }
    cap = kt_json_get(j, "cap_usd");
    if (kt_json_is_number(cap))
        snprintf(err, sizeof err, ", monthly cap $%.2f", kt_json_number(cap, 0.0));
    else
        snprintf(err, sizeof err, ", no cap set");
    snprintf(summary, sizeof summary, "Today $%.4f; last %.0f days $%.4f%s",
             kt_json_number(kt_json_get(j, "today_usd"), 0.0), kt_json_number(kt_json_get(j, "days"), 30.0),
             kt_json_number(kt_json_get(j, "period_usd"), 0.0), err);
    kt_dialog_set_summary(d, summary);
    kt_json_free(j);
    return d;
}

static kt_dialog *build_logs(kt_presenter *p)
{
    kt_json *j;
    char err[160];
    api_rc rc = api_call(p, "GET", "/kiko/v1/logs?lines=200", NULL, &j, err, sizeof err);
    kt_dialog *d;
    const kt_json *arr;
    kt_sb sb;
    char *text;
    size_t i;

    if (rc != API_OK) return problem_dialog(p, "logs", "kiko - Logs", rc, err);
    d = new_dialog("logs", "kiko - Logs");
    if (!d) { kt_json_free(j); return NULL; }
    kt_sb_init(&sb);
    arr = kt_json_get(j, "lines");
    for (i = 0; i < kt_json_count(arr); i++) {
        /* The server redacts; redact again here as a last line of defence. */
        char *clean = kt_mask_redact(kt_json_string(kt_json_at(arr, i), ""));
        kt_sb_append(&sb, clean);
        kt_sb_append(&sb, "\n");
        free(clean);
    }
    text = kt_sb_take(&sb);
    kt_dialog_set_text(d, text ? text : "");
    free(text);
    kt_dialog_set_summary(d, "Last 200 log lines (secrets redacted).");
    kt_json_free(j);
    return d;
}

static kt_dialog *build_status(kt_presenter *p)
{
    kt_http_resp r;
    kt_json *j = NULL, *st = NULL;
    char err[160];
    kt_sb sb;
    char *text;
    kt_dialog *d = new_dialog("status", "kiko - Status");
    int rc;
    api_rc arc;

    if (!d) return NULL;
    kt_sb_init(&sb);
    memset(&r, 0, sizeof r);
    rc = http_call(p, "GET", "/health", NULL, &r);
    kt_sb_printf(&sb, "Address: http://127.0.0.1:%d\n", p->port);
    if (rc != 0) {
        kt_sb_append(&sb, "Health: no answer (kiko is not running)\n");
    } else {
        j = kt_json_parse(r.body, r.body_len);
        kt_sb_printf(&sb, "Health: HTTP %d, status \"%s\"\n", r.status,
                     kt_json_string(kt_json_get(j, "status"), "?"));
    }
    kt_json_free(j);
    kt_http_resp_free(&r);
    if (rc == 0) {
        arc = api_call(p, "GET", "/kiko/v1/status", NULL, &st, err, sizeof err);
        if (arc == API_OK) {
            double up = kt_json_number(kt_json_get(st, "uptime_s"), -1.0);
            kt_sb_printf(&sb, "Version: %s\n", kt_json_string(kt_json_get(st, "version"), "?"));
            if (up >= 0.0)
                kt_sb_printf(&sb, "Uptime: %dh %dm %ds\n", (int)(up / 3600.0), (int)(up / 60.0) % 60,
                             (int)up % 60);
            kt_sb_printf(&sb, "Database: %s\n", kt_json_string(kt_json_get(st, "db_path"), "?"));
            kt_sb_printf(&sb, "Models enabled: %.0f\n", kt_json_number(kt_json_get(st, "models_enabled"), 0.0));
            kt_sb_printf(&sb, "Management API: available (v%.0f)\n",
                         kt_json_number(kt_json_get(st, "api_version"), 1.0));
        } else {
            kt_sb_append(&sb, "Management API: ");
            problem_text(p, arc, err, &sb);
            kt_sb_append(&sb, "\nOnly the /health state is shown.\n");
        }
        kt_json_free(st);
    }
    text = kt_sb_take(&sb);
    kt_dialog_set_text(d, text ? text : "");
    free(text);
    return d;
}

static kt_dialog *build_settings(kt_presenter *p)
{
    kt_sb sb;
    char *text;
    kt_dialog *d = new_dialog("settings", "kiko - Settings");
    if (!d) return NULL;
    kt_sb_init(&sb);
    kt_sb_printf(&sb, "kiko address: http://127.0.0.1:%d\n", p->port);
    kt_sb_printf(&sb, "Poll interval: %d ms\n", p->poll_interval_ms);
    kt_sb_printf(&sb, "Start command: %s\n", p->command_text);
    kt_sb_printf(&sb, "API token: %s (the token itself is never shown)\n",
                 p->token ? p->token_source : "not set");
    kt_sb_append(&sb, "\nSettings are read at start-up from the environment (KIKO_PORT, KIKO_API_TOKEN,\n"
                      "KIKO_TRAY_COMMAND on Windows).  Editing here is not available yet.\n");
    text = kt_sb_take(&sb);
    kt_dialog_set_text(d, text ? text : "");
    free(text);
    return d;
}

static kt_dialog *build_about(kt_presenter *p)
{
    kt_sb sb;
    char *text;
    kt_dialog *d = kt_dialog_new("about", "About kiko-tray");
    if (!d) return NULL;
    kt_sb_init(&sb);
    kt_sb_printf(&sb, "kiko-tray %s\n\nNative tray for kiko (model router).\n", p->version);
    kt_sb_append(&sb, "Author: Peter Kokosharov <peter.kokosharov@gmail.com>\nLicense: MIT\n");
    text = kt_sb_take(&sb);
    kt_dialog_set_text(d, text ? text : "");
    free(text);
    return d;
}

typedef struct { kt_presenter *p; char kind[16]; } dialog_job;
typedef struct { kt_presenter *p; kt_dialog *d; } ui_dialog;

static void ui_show_dialog(void *arg)
{
    ui_dialog *u = (ui_dialog *)arg;
    u->p->view.show_dialog(u->p->view.ctx, u->d);
    kt_dialog_free(u->d);
    free(u);
}

static void dialog_run(void *arg)
{
    dialog_job *j = (dialog_job *)arg;
    kt_presenter *p = j->p;
    kt_dialog *d = NULL;
    ui_dialog *u;

    if (strcmp(j->kind, KT_ID_STATUS) == 0) d = build_status(p);
    else if (strcmp(j->kind, KT_ID_LOGS) == 0) d = build_logs(p);
    else if (strcmp(j->kind, KT_ID_MODELS) == 0) d = build_models(p);
    else if (strcmp(j->kind, KT_ID_CLASSES) == 0) d = build_classes(p);
    else if (strcmp(j->kind, KT_ID_KEYS) == 0) d = build_keys(p);
    else if (strcmp(j->kind, KT_ID_COSTS) == 0) d = build_costs(p);
    else if (strcmp(j->kind, KT_ID_SETTINGS) == 0) d = build_settings(p);
    else if (strcmp(j->kind, KT_ID_ABOUT) == 0) d = build_about(p);
    if (d) {
        u = (ui_dialog *)calloc(1, sizeof *u);
        if (u) {
            u->p = p;
            u->d = d;
            post(p, ui_show_dialog, u);
        } else {
            kt_dialog_free(d);
        }
    }
    free(j);
}

static void open_dialog(kt_presenter *p, const char *kind)
{
    dialog_job *j = (dialog_job *)calloc(1, sizeof *j);
    if (!j) return;
    j->p = p;
    snprintf(j->kind, sizeof j->kind, "%s", kind);
    if (kt_worker_submit(p->worker, dialog_run, action_drop, j) != 0) free(j);
}

/* ---------------------------------------------------------------- set-class hook */

typedef struct { kt_presenter *p; char model[96]; char cls[40]; } class_job;

/* model = [A-Za-z0-9._:-]+ (model_chars = 1); class = [A-Z0-9_]+ (model_chars = 0), as in API-CONTRACT.md */
static int safe_chars(const char *s, int model_chars)
{
    size_t i;
    if (!s || !s[0]) return 0;
    for (i = 0; s[i]; i++) {
        char c = s[i];
        int upper = c >= 'A' && c <= 'Z';
        int digit = c >= '0' && c <= '9';
        int ok = upper || digit || c == '_';
        if (model_chars) ok = ok || (c >= 'a' && c <= 'z') || c == '-' || c == '.' || c == ':';
        if (!ok) return 0;
    }
    return 1;
}

static void class_run(void *arg)
{
    class_job *j = (class_job *)arg;
    char path[160], body[96], err[160], msg[256];
    kt_json *out;
    api_rc rc;
    kt_sb sb;
    char *s;

    snprintf(path, sizeof path, "/kiko/v1/models/%s/class", j->model);
    snprintf(body, sizeof body, "{\"class\":\"%s\"}", j->cls);
    rc = api_call(j->p, "POST", path, body, &out, err, sizeof err);
    if (rc == API_OK) {
        snprintf(msg, sizeof msg, "Model %s is now class %s", j->model, j->cls);
        kt_json_free(out);
    } else {
        kt_sb_init(&sb);
        problem_text(j->p, rc, err, &sb);
        s = kt_sb_take(&sb);
        snprintf(msg, sizeof msg, "%s", s ? s : "failed");
        free(s);
    }
    post_notify(j->p, msg);
    free(j);
}

int kt_presenter_set_model_class(kt_presenter *p, const char *model, const char *model_class)
{
    class_job *j;
    if (!safe_chars(model, 1) || !safe_chars(model_class, 0) || strspn(model, ".") == strlen(model)   /* "." and ".." */
        || strlen(model) >= sizeof j->model
        || strlen(model_class) >= sizeof j->cls)
        return -1;
    j = (class_job *)calloc(1, sizeof *j);
    if (!j) return -1;
    j->p = p;
    snprintf(j->model, sizeof j->model, "%s", model);
    snprintf(j->cls, sizeof j->cls, "%s", model_class);
    if (kt_worker_submit(p->worker, class_run, action_drop, j) != 0) { free(j); return -1; }
    return 0;
}

/* ---------------------------------------------------------------- events: view -> presenter */

static void ev_menu_item(void *ctx, const char *id)
{
    kt_presenter *p = (kt_presenter *)ctx;
    if (!id || !id[0]) return;
    if (strcmp(id, KT_ID_START) == 0) start_action(p, KT_ACT_START);
    else if (strcmp(id, KT_ID_STOP) == 0) start_action(p, KT_ACT_STOP);
    else if (strcmp(id, KT_ID_RESTART) == 0) start_action(p, KT_ACT_RESTART);
    else if (strcmp(id, KT_ID_QUIT) == 0) p->view.quit(p->view.ctx);   /* leaves kiko running */
    else open_dialog(p, id);   /* unknown ids are ignored by dialog_run */
}

static void ev_default_action(void *ctx) { open_dialog((kt_presenter *)ctx, KT_ID_STATUS); }

static void ev_dialog_button(void *ctx, const char *kind, const char *button_id)
{
    kt_presenter *p = (kt_presenter *)ctx;
    if (!kind || !button_id) return;
    if (strcmp(button_id, "refresh") == 0) open_dialog(p, kind);
    else if (strcmp(button_id, "open_keystore") == 0) p->view.open_key_store(p->view.ctx);
}

static void ev_dialog_closed(void *ctx, const char *kind) { (void)ctx; (void)kind; }

const kt_events *kt_presenter_events(kt_presenter *p) { return &p->events; }

/* ---------------------------------------------------------------- lifecycle */

kt_presenter *kt_presenter_new(const kt_view *view, const kt_host *host, const kt_config *cfg)
{
    kt_presenter *p;
    if (!view || !cfg || !view->post_to_ui || !view->set_state || !view->set_menu || !view->show_dialog
        || !view->notify || !view->quit || !view->bind)
        return NULL;
    p = (kt_presenter *)calloc(1, sizeof *p);
    if (!p) return NULL;
    p->view = *view;
    if (host) p->host = *host;
    p->port = cfg->port > 0 ? cfg->port : 4000;
    p->token = cfg->api_token && cfg->api_token[0] ? kt_strdup(cfg->api_token) : NULL;
    p->token_source = kt_strdup(cfg->token_source ? cfg->token_source : "none");
    p->command_text = kt_strdup(cfg->command_text ? cfg->command_text : "(not configured)");
    p->version = kt_strdup(cfg->version ? cfg->version : "0");
    p->poll_interval_ms = cfg->poll_interval_ms > 0 ? cfg->poll_interval_ms : 5000;
    p->start_grace_ms = cfg->start_grace_ms > 0 ? cfg->start_grace_ms : 30000;
    p->http = cfg->http ? cfg->http : kt_http_request;
    p->state = KT_STATE_STARTING;
    snprintf(p->tooltip, sizeof p->tooltip, "KIKO: checking...");
    p->events.ctx = p;
    p->events.on_menu_item = ev_menu_item;
    p->events.on_default_action = ev_default_action;
    p->events.on_dialog_button = ev_dialog_button;
    p->events.on_dialog_closed = ev_dialog_closed;
    kt_mutex_init(&p->mu);
    kt_cond_init(&p->poll_wake);
    kt_net_startup();
    p->worker = kt_worker_new();
    if (!p->worker || !p->token_source || !p->command_text || !p->version) {
        kt_presenter_free(p);
        return NULL;
    }
    return p;
}

int kt_presenter_start(kt_presenter *p)
{
    p->view.bind(p->view.ctx, &p->events);
    p->view.set_state(p->view.ctx, p->state, p->tooltip);
    rebuild_menu(p);
    if (kt_thread_start(&p->poll_th, poll_thread_main, p) != 0) return -1;
    p->poll_started = 1;
    return 0;
}

void kt_presenter_stop(kt_presenter *p)
{
    if (!p) return;
    kt_mutex_lock(&p->mu);
    p->stop = 1;
    kt_cond_broadcast(&p->poll_wake);
    kt_mutex_unlock(&p->mu);
    if (p->poll_started) {
        kt_thread_join(&p->poll_th);
        p->poll_started = 0;
    }
}

void kt_presenter_free(kt_presenter *p)
{
    if (!p) return;
    kt_presenter_stop(p);
    kt_worker_free(p->worker);
    kt_cond_destroy(&p->poll_wake);
    kt_mutex_destroy(&p->mu);
    if (p->token) kt_secure_zero(p->token, strlen(p->token));
    free(p->token);
    free(p->token_source);
    free(p->command_text);
    free(p->version);
    free(p);
}

int kt_presenter_wait_idle(kt_presenter *p, unsigned timeout_ms) { return kt_worker_wait_idle(p->worker, timeout_ms); }

kt_state kt_presenter_state(kt_presenter *p) { return p->state; }
