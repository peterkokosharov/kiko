/* SPDX-License-Identifier: MIT
 * Author: Peter Kokosharov <peter.kokosharov@gmail.com>
 * kt_presenter.h - the GUI-neutral brain of the tray: owns the menu, polls kiko's state, builds the
 * dialog contents, and runs every blocking call (HTTP, service control) on worker threads.
 *
 * Talks to kiko ONLY over HTTP+JSON on 127.0.0.1 (see docs/API-CONTRACT.md).  No SQLite, no files
 * read here, key values are never requested.
 *
 * Threading: the presenter's state is touched only on the UI thread (kt_events callbacks and the
 * functions posted through kt_view.post_to_ui).  Workers only use immutable config and post results.
 * kt_presenter_free() must be called after the view's run() has returned.
 */
#ifndef KT_PRESENTER_H
#define KT_PRESENTER_H

#include <stddef.h>

#include "kt_http.h"
#include "kt_view.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum { KT_ACT_START = 0, KT_ACT_STOP, KT_ACT_RESTART } kt_action;

/* OS-specific process/service control, supplied by the GUI layer's main.  Blocking: it runs on a
 * worker thread.  Returns 0 on success; msg receives a short human text either way. */
typedef struct kt_host {
    void *ctx;
    int (*service)(void *ctx, kt_action action, char *msg, size_t msgsz);
} kt_host;

typedef struct kt_config {
    int         port;                /* kiko's loopback port, default 4000 */
    const char *api_token;           /* Bearer token for /kiko/v1/*; NULL = none.  Never logged. */
    const char *token_source;        /* text for the Settings dialog, e.g. "env KIKO_API_TOKEN" */
    const char *command_text;        /* how kiko is started, for the Settings dialog */
    const char *version;             /* tray version string for About */
    int         poll_interval_ms;    /* default 5000 */
    int         start_grace_ms;      /* STARTING shown this long after a start; default 30000 */
    kt_http_fn  http;                /* NULL = kt_http_request; tests inject a fake */
} kt_config;

typedef struct kt_presenter kt_presenter;

/* Strings in cfg are copied.  view and host are copied by value.  NULL on failure. */
kt_presenter *kt_presenter_new(const kt_view *view, const kt_host *host, const kt_config *cfg);
/* Binds to the view, shows the initial menu/state, starts the polling thread.  0 on success. */
int           kt_presenter_start(kt_presenter *p);
/* Stops polling.  Idempotent. */
void          kt_presenter_stop(kt_presenter *p);
void          kt_presenter_free(kt_presenter *p);

/* The callbacks the view must call (also what tests drive). */
const kt_events *kt_presenter_events(kt_presenter *p);

/* Hook for the future "change class" editor: asynchronous POST /kiko/v1/models/{name}/class.
 * The result arrives as a notification.  0 if queued, -1 if name/class are not acceptable. */
int  kt_presenter_set_model_class(kt_presenter *p, const char *model, const char *model_class);

/* Test helpers. */
int      kt_presenter_wait_idle(kt_presenter *p, unsigned timeout_ms);   /* 0 = worker idle */
kt_state kt_presenter_state(kt_presenter *p);                            /* UI thread only */
void     kt_presenter_poll_now(kt_presenter *p);                         /* queue one poll job */

/* Menu item ids (stable; GUI layers need not know them). */
#define KT_ID_START    "start"
#define KT_ID_STOP     "stop"
#define KT_ID_RESTART  "restart"
#define KT_ID_STATUS   "status"
#define KT_ID_LOGS     "logs"
#define KT_ID_MODELS   "models"
#define KT_ID_CLASSES  "classes"
#define KT_ID_KEYS     "keys"
#define KT_ID_COSTS    "costs"
#define KT_ID_SETTINGS "settings"
#define KT_ID_ABOUT    "about"
#define KT_ID_QUIT     "quit"

#ifdef __cplusplus
}
#endif

#endif
