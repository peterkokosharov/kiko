/* SPDX-License-Identifier: MIT
 * Author: Peter Kokosharov <peter.kokosharov@gmail.com>
 * kt_view.h - the GUI-neutral contract between the common core and a per-OS GUI layer.
 *
 * A GUI layer (native/win32, native/gtk, later TQt in C++) fills in a kt_view (struct of function
 * pointers) and receives a kt_events (the presenter's callbacks) through bind().
 *
 * THREADING RULE: every kt_view function and every kt_events callback runs on the UI thread, except
 * post_to_ui and quit, which may be called from ANY thread.  Blocking work (HTTP, process control)
 * runs on the presenter's worker threads; results come back through post_to_ui.
 *
 * OWNERSHIP: pointers handed to a kt_view function (menu items, dialog, strings) are valid only for
 * the duration of the call.  The view copies what it keeps.  All strings are UTF-8.
 */
#ifndef KT_VIEW_H
#define KT_VIEW_H

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    KT_STATE_STARTING = 0,   /* blue   */
    KT_STATE_OK,             /* green  */
    KT_STATE_DEGRADED,       /* orange: answers, but /health is not ok */
    KT_STATE_DOWN            /* red    */
} kt_state;

typedef struct {
    const char *id;       /* what on_menu_item receives; NULL/"" for separators and the status line */
    const char *label;    /* UTF-8 */
    int         enabled;
    int         separator;
} kt_menu_item;

/* ---- dialog model ---- */

typedef struct {
    char *id;
    char *label;
} kt_button;

/* Layout, top to bottom: summary label; body = table (columns + rows) when ncols > 0, otherwise a
 * read-only multi-line text; footer label; button row (buttons + a Close button the view adds).
 * "kind" is unique: showing a kind that is already open updates and re-focuses it. */
typedef struct kt_dialog {
    char       *kind;
    char       *title;
    char       *summary;
    char       *text;
    char       *footer;
    int         ncols;
    char      **columns;
    int         nrows;
    char      **cells;      /* nrows * ncols, row-major */
    int         nbuttons;
    kt_button  *buttons;
    int         rowcap;
    int         btncap;
} kt_dialog;

kt_dialog *kt_dialog_new(const char *kind, const char *title);   /* strings are copied */
void       kt_dialog_free(kt_dialog *d);
void       kt_dialog_set_summary(kt_dialog *d, const char *s);
void       kt_dialog_set_text(kt_dialog *d, const char *s);
void       kt_dialog_set_footer(kt_dialog *d, const char *s);
/* Columns must be added before any row. */
int        kt_dialog_add_column(kt_dialog *d, const char *name);
/* cells: exactly ncols strings (missing ones may be NULL). */
int        kt_dialog_add_row(kt_dialog *d, const char *const *cells);
int        kt_dialog_add_button(kt_dialog *d, const char *id, const char *label);

/* ---- events: view -> presenter ---- */

typedef struct kt_events {
    void *ctx;
    void (*on_menu_item)(void *ctx, const char *item_id);
    void (*on_default_action)(void *ctx);                                  /* tray icon double-click */
    void (*on_dialog_button)(void *ctx, const char *kind, const char *button_id);
    void (*on_dialog_closed)(void *ctx, const char *kind);
} kt_events;

/* ---- the view: presenter -> GUI layer ---- */

typedef void (*kt_ui_fn)(void *arg);

typedef struct kt_view {
    void       *ctx;
    const char *key_store_label;   /* label of the Keys dialog wallet button; NULL = no button */

    void (*bind)(void *ctx, const kt_events *events);
    int  (*claim_single_instance)(void *ctx);                 /* 0 = another tray already runs */
    void (*set_state)(void *ctx, kt_state state, const char *tooltip);
    void (*set_menu)(void *ctx, const kt_menu_item *items, int n);
    void (*show_dialog)(void *ctx, const kt_dialog *dialog);  /* create, or update + focus */
    void (*notify)(void *ctx, const char *text);              /* balloon / desktop notification */
    void (*open_key_store)(void *ctx);                        /* Credential Manager / seahorse */
    void (*post_to_ui)(void *ctx, kt_ui_fn fn, void *arg);    /* ANY thread; fn runs on the UI thread */
    void (*run)(void *ctx);                                   /* event loop; returns after quit() */
    void (*quit)(void *ctx);                                  /* ANY thread; makes run() return */
} kt_view;

#ifdef __cplusplus
}
#endif

#endif
