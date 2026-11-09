/* SPDX-License-Identifier: MIT
 * Author: Peter Kokosharov <peter.kokosharov@gmail.com>
 * fake_view.h - a GUI-free kt_view plus a scriptable fake HTTP backend for the presenter tests.
 * "UI thread" = the test's main thread: posted functions only run when the test calls fv_pump().
 */
#ifndef FAKE_VIEW_H
#define FAKE_VIEW_H

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "kt_http.h"
#include "kt_presenter.h"
#include "kt_thread.h"

#ifndef _WIN32
#include <time.h>
#endif

static void sleep_ms(unsigned ms)
{
#ifdef _WIN32
    Sleep(ms);
#else
    struct timespec ts;
    ts.tv_sec = ms / 1000;
    ts.tv_nsec = (long)(ms % 1000) * 1000000L;
    nanosleep(&ts, NULL);
#endif
}

static unsigned long cur_tid(void)
{
#ifdef _WIN32
    return (unsigned long)GetCurrentThreadId();
#else
    return (unsigned long)pthread_self();
#endif
}

/* ---- fake backend ---- */

typedef struct { const char *path; int rc; int status; const char *body; } fake_route;

#define MAX_ROUTES 16
static fake_route g_routes[MAX_ROUTES];
static int        g_nroutes;
static kt_mutex   g_http_mu;
static unsigned long g_ui_tid;
static int        g_http_on_ui_thread;     /* must stay 0 */
static int        g_health_got_token;      /* must stay 0 */
static int        g_api_missing_token;     /* count of /kiko/ calls without the token */
static char       g_last_post_path[160];
static char       g_last_post_body[160];

static void fake_reset(void)
{
    g_nroutes = 0;
    g_http_on_ui_thread = g_health_got_token = g_api_missing_token = 0;
    g_last_post_path[0] = g_last_post_body[0] = '\0';
}

/* Routes match the exact path (query string included).  An unrouted path answers HTTP 404. */
static void fake_set(const char *path, int rc, int status, const char *body)
{
    int i;
    kt_mutex_lock(&g_http_mu);
    for (i = 0; i < g_nroutes; i++)
        if (strcmp(g_routes[i].path, path) == 0) break;
    if (i == g_nroutes && g_nroutes < MAX_ROUTES) g_nroutes++;
    g_routes[i].path = path;
    g_routes[i].rc = rc;
    g_routes[i].status = status;
    g_routes[i].body = body;
    kt_mutex_unlock(&g_http_mu);
}

#define FAKE_TOKEN "SECRETTOKEN-0123456789"

static int fake_http(const char *method, int port, const char *path, const char *bearer,
                     const char *json_body, int timeout_ms, kt_http_resp *out)
{
    int i, rc = 0, status = 404;
    const char *body = "{\"error\":{\"message\":\"no route\"}}";
    (void)port; (void)timeout_ms;
    memset(out, 0, sizeof *out);
    kt_mutex_lock(&g_http_mu);
    if (cur_tid() == g_ui_tid) g_http_on_ui_thread = 1;
    if (strcmp(path, "/health") == 0 && bearer) g_health_got_token = 1;
    if (strncmp(path, "/kiko/", 6) == 0 && (!bearer || strcmp(bearer, FAKE_TOKEN) != 0)) g_api_missing_token++;
    if (strcmp(method, "POST") == 0) {
        snprintf(g_last_post_path, sizeof g_last_post_path, "%s", path);
        snprintf(g_last_post_body, sizeof g_last_post_body, "%s", json_body ? json_body : "");
    }
    for (i = 0; i < g_nroutes; i++) {
        if (strcmp(g_routes[i].path, path) == 0) {
            rc = g_routes[i].rc;
            status = g_routes[i].status;
            body = g_routes[i].body;
        }
    }
    kt_mutex_unlock(&g_http_mu);
    if (rc != 0) return rc;
    out->status = status;
    out->body = kt_strdup(body);
    out->body_len = strlen(body);
    return 0;
}

/* ---- fake view ---- */

#define MAX_CAPTURE 32

typedef struct {
    char kind[16];
    char summary[512];
    char text[2048];
    char cells[2048];     /* cells joined with '|', rows ended by a newline */
    char buttons[128];    /* ids joined with ',' */
    int  nrows;
    int  ncols;
} captured_dialog;

typedef struct { kt_ui_fn fn; void *arg; } posted;

typedef struct {
    kt_mutex          mu;
    posted            q[256];
    int               nq;
    kt_state          state;
    char              tooltip[200];
    int               set_state_calls;
    char              menu_ids[24][16];
    char              menu_labels[24][48];
    int               menu_enabled[24];
    int               menu_sep[24];
    int               menu_n;
    captured_dialog   dlg[MAX_CAPTURE];
    int               ndlg;
    char              notes[16][300];
    int               nnotes;
    int               quit_called;
    int               keystore_opened;
    int               bound;
    const kt_events  *events;
} fake_view;

static void fv_bind(void *c, const kt_events *e) { fake_view *v = (fake_view *)c; v->events = e; v->bound = 1; }
static int fv_claim(void *c) { (void)c; return 1; }

static void fv_set_state(void *c, kt_state s, const char *tip)
{
    fake_view *v = (fake_view *)c;
    v->state = s;
    snprintf(v->tooltip, sizeof v->tooltip, "%s", tip);
    v->set_state_calls++;
}

static void fv_set_menu(void *c, const kt_menu_item *it, int n)
{
    fake_view *v = (fake_view *)c;
    int i;
    v->menu_n = n > 24 ? 24 : n;
    for (i = 0; i < v->menu_n; i++) {
        snprintf(v->menu_ids[i], sizeof v->menu_ids[i], "%s", it[i].id ? it[i].id : "");
        snprintf(v->menu_labels[i], sizeof v->menu_labels[i], "%s", it[i].label ? it[i].label : "");
        v->menu_enabled[i] = it[i].enabled;
        v->menu_sep[i] = it[i].separator;
    }
}

static void fv_show_dialog(void *c, const kt_dialog *d)
{
    fake_view *v = (fake_view *)c;
    captured_dialog *k;
    int i, r, col;
    size_t pos = 0;
    if (v->ndlg >= MAX_CAPTURE) return;
    k = &v->dlg[v->ndlg++];
    memset(k, 0, sizeof *k);
    snprintf(k->kind, sizeof k->kind, "%s", d->kind);
    snprintf(k->summary, sizeof k->summary, "%s", d->summary);
    snprintf(k->text, sizeof k->text, "%s", d->text);
    k->nrows = d->nrows;
    k->ncols = d->ncols;
    for (r = 0; r < d->nrows; r++) {
        for (col = 0; col < d->ncols; col++) {
            int n = snprintf(k->cells + pos, sizeof k->cells - pos, "%s%s", col ? "|" : "",
                             d->cells[r * d->ncols + col]);
            if (n > 0 && pos + (size_t)n < sizeof k->cells) pos += (size_t)n;
        }
        if (pos + 1 < sizeof k->cells) { k->cells[pos++] = '\n'; k->cells[pos] = '\0'; }
    }
    for (i = 0; i < d->nbuttons; i++) {
        strncat(k->buttons, i ? "," : "", sizeof k->buttons - strlen(k->buttons) - 1);
        strncat(k->buttons, d->buttons[i].id, sizeof k->buttons - strlen(k->buttons) - 1);
    }
}

static void fv_notify(void *c, const char *t)
{
    fake_view *v = (fake_view *)c;
    if (v->nnotes < 16) snprintf(v->notes[v->nnotes++], sizeof v->notes[0], "%s", t);
}

static void fv_open_key_store(void *c) { ((fake_view *)c)->keystore_opened = 1; }

static void fv_post(void *c, kt_ui_fn fn, void *arg)
{
    fake_view *v = (fake_view *)c;
    kt_mutex_lock(&v->mu);
    if (v->nq < 256) { v->q[v->nq].fn = fn; v->q[v->nq].arg = arg; v->nq++; }
    kt_mutex_unlock(&v->mu);
}

static void fv_run(void *c) { (void)c; }
static void fv_quit(void *c) { ((fake_view *)c)->quit_called = 1; }

static void fv_init(fake_view *v, kt_view *out, const char *keystore_label)
{
    memset(v, 0, sizeof *v);
    kt_mutex_init(&v->mu);
    memset(out, 0, sizeof *out);
    out->ctx = v;
    out->key_store_label = keystore_label;
    out->bind = fv_bind;
    out->claim_single_instance = fv_claim;
    out->set_state = fv_set_state;
    out->set_menu = fv_set_menu;
    out->show_dialog = fv_show_dialog;
    out->notify = fv_notify;
    out->open_key_store = fv_open_key_store;
    out->post_to_ui = fv_post;
    out->run = fv_run;
    out->quit = fv_quit;
    g_ui_tid = cur_tid();
}

/* Runs everything posted so far on the calling ("UI") thread; returns how many ran. */
static int fv_pump(fake_view *v)
{
    int n = 0;
    for (;;) {
        posted p;
        kt_mutex_lock(&v->mu);
        if (v->nq == 0) { kt_mutex_unlock(&v->mu); break; }
        p = v->q[0];
        memmove(&v->q[0], &v->q[1], (size_t)(v->nq - 1) * sizeof v->q[0]);
        v->nq--;
        kt_mutex_unlock(&v->mu);
        p.fn(p.arg);
        n++;
    }
    return n;
}

#endif
