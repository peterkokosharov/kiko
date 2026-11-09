/* SPDX-License-Identifier: MIT
 * Author: Peter Kokosharov <peter.kokosharov@gmail.com>
 *
 * gtk_view.c - GTK3 tray for Linux Mint (Cinnamon).  Tray icon: XAppStatusIcon (libxapp) when the
 * build found it, else GtkStatusIcon.  Menu and dialogs are plain GTK3 widgets.  State icons are
 * drawn in memory by the common core (kt_icon) and, for XApp, saved once as PNG files in the cache dir.
 * Everything except post_to_ui and quit runs on the GTK main thread.
 */
#include "gtk_internal.h"

#include <fcntl.h>
#include <string.h>
#include <sys/file.h>
#include <unistd.h>

#include "kt_icon.h"
#include "kt_util.h"

#ifdef KT_HAVE_XAPP
#include <libxapp/xapp-status-icon.h>
#endif

typedef struct { kt_ui_fn fn; void *arg; } post_item;

static const kt_events *g_events;
static GtkStatusIcon   *g_sicon;
#ifdef KT_HAVE_XAPP
static XAppStatusIcon  *g_xicon;
#endif
static GdkPixbuf       *g_pix[4];
static gchar           *g_png[4];
static GtkWidget       *g_menu;
static int              g_lock_fd = -1;

static void free_pixels(guchar *pixels, gpointer data) { (void)data; g_free(pixels); }

static GdkPixbuf *make_pixbuf(kt_state st, int size)
{
    guchar *px = (guchar *)g_malloc((gsize)size * (gsize)size * 4);
    kt_icon_render(size, st, 0, px);   /* RGBA, straight alpha */
    return gdk_pixbuf_new_from_data(px, GDK_COLORSPACE_RGB, TRUE, 8, size, size, size * 4, free_pixels, NULL);
}

static void build_icons(void)
{
    int i;
    gchar *dir = g_build_filename(g_get_user_cache_dir(), "kiko-tray", NULL);
    g_mkdir_with_parents(dir, 0700);
    for (i = 0; i < 4; i++) {
        GdkPixbuf *big = make_pixbuf((kt_state)i, 48);
        g_pix[i] = make_pixbuf((kt_state)i, 22);
        g_png[i] = g_strdup_printf("%s/state-%d.png", dir, i);
        gdk_pixbuf_save(big, g_png[i], "png", NULL, NULL);   /* XApp wants a path or a theme name */
        g_object_unref(big);
    }
    g_free(dir);
}

/* ---------------------------------------------------------------- tray icon */

static void on_default_action(void)
{
    if (g_events && g_events->on_default_action) g_events->on_default_action(g_events->ctx);
}

#ifdef KT_HAVE_XAPP
static void on_xapp_activate(XAppStatusIcon *icon, guint button, guint time, gpointer data)
{
    (void)icon; (void)time; (void)data;
    if (button == GDK_BUTTON_PRIMARY) on_default_action();
}
#endif

static void on_sicon_popup(GtkStatusIcon *icon, guint button, guint time, gpointer data)
{
    (void)data;
    if (g_menu) gtk_menu_popup(GTK_MENU(g_menu), NULL, NULL, gtk_status_icon_position_menu, icon, button, time);
}

static void on_sicon_activate(GtkStatusIcon *icon, gpointer data) { (void)icon; (void)data; on_default_action(); }

static void create_tray_icon(void)
{
#ifdef KT_HAVE_XAPP
    g_xicon = xapp_status_icon_new();
    if (g_xicon) {
        xapp_status_icon_set_name(g_xicon, "kiko-tray");
        xapp_status_icon_set_icon_name(g_xicon, g_png[KT_STATE_STARTING]);
        xapp_status_icon_set_tooltip_text(g_xicon, "KIKO");
        xapp_status_icon_set_visible(g_xicon, TRUE);
        g_signal_connect(g_xicon, "activate", G_CALLBACK(on_xapp_activate), NULL);
        return;
    }
#endif
    g_sicon = gtk_status_icon_new_from_pixbuf(g_pix[KT_STATE_STARTING]);
    gtk_status_icon_set_title(g_sicon, "kiko-tray");
    gtk_status_icon_set_visible(g_sicon, TRUE);
    g_signal_connect(g_sicon, "popup-menu", G_CALLBACK(on_sicon_popup), NULL);
    g_signal_connect(g_sicon, "activate", G_CALLBACK(on_sicon_activate), NULL);
}

/* ---------------------------------------------------------------- kt_view: state, menu, notify */

static void v_bind(void *ctx, const kt_events *e) { (void)ctx; g_events = e; }

static int v_claim_single_instance(void *ctx)
{
    const char *rt = g_get_user_runtime_dir();
    gchar *path = g_build_filename(rt ? rt : g_get_tmp_dir(), "kiko-tray.lock", NULL);
    (void)ctx;
    g_lock_fd = open(path, O_CREAT | O_RDWR | O_CLOEXEC, 0600);
    g_free(path);
    if (g_lock_fd < 0) return 1;   /* cannot lock: do not block the user */
    if (flock(g_lock_fd, LOCK_EX | LOCK_NB) != 0) { close(g_lock_fd); g_lock_fd = -1; return 0; }
    return 1;
}

static void v_set_state(void *ctx, kt_state st, const char *tooltip)
{
    (void)ctx;
#ifdef KT_HAVE_XAPP
    if (g_xicon) {
        xapp_status_icon_set_icon_name(g_xicon, g_png[st]);
        xapp_status_icon_set_tooltip_text(g_xicon, tooltip);
        return;
    }
#endif
    if (g_sicon) {
        gtk_status_icon_set_from_pixbuf(g_sicon, g_pix[st]);
        gtk_status_icon_set_tooltip_text(g_sicon, tooltip);
    }
}

static void on_menu_activate(GtkMenuItem *item, gpointer data)
{
    const char *id = (const char *)g_object_get_data(G_OBJECT(item), "kt-id");
    (void)data;
    if (id && id[0] && g_events && g_events->on_menu_item) g_events->on_menu_item(g_events->ctx, id);
}

static void v_set_menu(void *ctx, const kt_menu_item *items, int n)
{
    GtkWidget *menu = gtk_menu_new();
    GtkWidget *old = g_menu;
    int i;
    (void)ctx;
    for (i = 0; i < n; i++) {
        GtkWidget *w;
        if (items[i].separator) {
            w = gtk_separator_menu_item_new();
        } else {
            w = gtk_menu_item_new_with_label(items[i].label ? items[i].label : "");
            gtk_widget_set_sensitive(w, items[i].enabled ? TRUE : FALSE);
            g_object_set_data_full(G_OBJECT(w), "kt-id", g_strdup(items[i].id ? items[i].id : ""), g_free);
            g_signal_connect(w, "activate", G_CALLBACK(on_menu_activate), NULL);
        }
        gtk_menu_shell_append(GTK_MENU_SHELL(menu), w);
    }
    gtk_widget_show_all(menu);
    g_menu = (GtkWidget *)g_object_ref_sink(menu);   /* we own one reference, whatever the tray does with it */
#ifdef KT_HAVE_XAPP
    if (g_xicon) xapp_status_icon_set_secondary_menu(g_xicon, GTK_MENU(g_menu));
#endif
    if (old) g_object_unref(old);
}

static void spawn_quiet(char **argv)
{
    GError *err = NULL;
    if (!g_spawn_async(NULL, argv, NULL,
                       G_SPAWN_SEARCH_PATH | G_SPAWN_STDOUT_TO_DEV_NULL | G_SPAWN_STDERR_TO_DEV_NULL, NULL, NULL,
                       NULL, &err))
        g_clear_error(&err);
}

static void v_notify(void *ctx, const char *text)
{
    char *argv[7];
    (void)ctx;
    argv[0] = (char *)"notify-send";
    argv[1] = (char *)"-a";
    argv[2] = (char *)"kiko-tray";
    argv[3] = (char *)"--";   /* the text may start with a dash */
    argv[4] = (char *)"kiko";
    argv[5] = (char *)text;
    argv[6] = NULL;
    spawn_quiet(argv);
}

static void v_open_key_store(void *ctx)
{
    GError *err = NULL;
    char *argv[2];
    (void)ctx;
    argv[0] = (char *)"seahorse";   /* Passwords and Keys */
    argv[1] = NULL;
    if (!g_spawn_async(NULL, argv, NULL, G_SPAWN_SEARCH_PATH, NULL, NULL, NULL, &err)) {
        v_notify(NULL, "Could not start seahorse (sudo apt install seahorse)");
        g_clear_error(&err);
    }
}

static gboolean post_cb(gpointer data)
{
    post_item *it = (post_item *)data;
    it->fn(it->arg);
    g_free(it);
    return G_SOURCE_REMOVE;
}

static void v_post_to_ui(void *ctx, kt_ui_fn fn, void *arg)
{
    post_item *it = g_new(post_item, 1);
    (void)ctx;
    it->fn = fn;
    it->arg = arg;
    g_idle_add(post_cb, it);   /* g_idle_add is thread safe */
}

static gboolean quit_cb(gpointer data) { (void)data; gtk_main_quit(); return G_SOURCE_REMOVE; }
static void v_run(void *ctx) { (void)ctx; gtk_main(); }
static void v_quit(void *ctx) { (void)ctx; g_idle_add(quit_cb, NULL); }

/* ---------------------------------------------------------------- dialogs */

typedef struct gdlg {
    GtkWidget   *win;
    char        *kind;
    struct gdlg *next;
} gdlg;

static gdlg *g_dialogs;

static void on_dialog_button(GtkButton *b, gpointer data)
{
    gdlg *d = (gdlg *)data;
    const char *id = (const char *)g_object_get_data(G_OBJECT(b), "kt-id");
    if (id && g_events && g_events->on_dialog_button) g_events->on_dialog_button(g_events->ctx, d->kind, id);
}

static void on_close_clicked(GtkButton *b, gpointer data) { (void)b; gtk_widget_destroy(GTK_WIDGET(data)); }

static void on_dialog_destroy(GtkWidget *w, gpointer data)
{
    gdlg *d = (gdlg *)data;
    gdlg **pp;
    (void)w;
    for (pp = &g_dialogs; *pp; pp = &(*pp)->next)
        if (*pp == d) { *pp = d->next; break; }
    if (g_events && g_events->on_dialog_closed) g_events->on_dialog_closed(g_events->ctx, d->kind);
    g_free(d->kind);
    g_free(d);
}

static GtkWidget *make_label(const char *text)
{
    GtkWidget *l = gtk_label_new(text);
    gtk_label_set_xalign(GTK_LABEL(l), 0.0f);
    gtk_label_set_line_wrap(GTK_LABEL(l), TRUE);
    gtk_label_set_selectable(GTK_LABEL(l), TRUE);
    return l;
}

static GtkWidget *make_table(const kt_dialog *m)
{
    GType *types = g_new(GType, m->ncols);
    GtkListStore *store;
    GtkWidget *tree;
    int c, r;

    for (c = 0; c < m->ncols; c++) types[c] = G_TYPE_STRING;
    store = gtk_list_store_newv(m->ncols, types);
    g_free(types);
    for (r = 0; r < m->nrows; r++) {
        GtkTreeIter it;
        gtk_list_store_append(store, &it);
        for (c = 0; c < m->ncols; c++) gtk_list_store_set(store, &it, c, m->cells[r * m->ncols + c], -1);
    }
    tree = gtk_tree_view_new_with_model(GTK_TREE_MODEL(store));
    g_object_unref(store);
    for (c = 0; c < m->ncols; c++) {
        GtkCellRenderer *cell = gtk_cell_renderer_text_new();
        GtkTreeViewColumn *col = gtk_tree_view_column_new_with_attributes(m->columns[c], cell, "text", c, NULL);
        gtk_tree_view_column_set_resizable(col, TRUE);
        gtk_tree_view_append_column(GTK_TREE_VIEW(tree), col);
    }
    return tree;
}

static GtkWidget *make_text(const kt_dialog *m)
{
    GtkWidget *tv = gtk_text_view_new();
    GtkTextBuffer *buf = gtk_text_view_get_buffer(GTK_TEXT_VIEW(tv));
    GtkTextIter end;
    gtk_text_view_set_editable(GTK_TEXT_VIEW(tv), FALSE);
    gtk_text_view_set_monospace(GTK_TEXT_VIEW(tv), TRUE);
    gtk_text_buffer_set_text(buf, m->text, -1);
    gtk_text_buffer_get_end_iter(buf, &end);
    gtk_text_view_scroll_to_iter(GTK_TEXT_VIEW(tv), &end, 0.0, FALSE, 0.0, 0.0);   /* newest line (logs) */
    return tv;
}

static GtkWidget *make_content(gdlg *d, const kt_dialog *m)
{
    GtkWidget *box = gtk_box_new(GTK_ORIENTATION_VERTICAL, 8);
    GtkWidget *scroll = gtk_scrolled_window_new(NULL, NULL);
    GtkWidget *row = gtk_box_new(GTK_ORIENTATION_HORIZONTAL, 8);
    GtkWidget *close = gtk_button_new_with_label("Close");
    int i;

    gtk_container_set_border_width(GTK_CONTAINER(box), 12);
    if (m->summary[0]) gtk_box_pack_start(GTK_BOX(box), make_label(m->summary), FALSE, FALSE, 0);
    gtk_container_add(GTK_CONTAINER(scroll), m->ncols > 0 ? make_table(m) : make_text(m));
    gtk_box_pack_start(GTK_BOX(box), scroll, TRUE, TRUE, 0);
    if (m->footer[0]) gtk_box_pack_start(GTK_BOX(box), make_label(m->footer), FALSE, FALSE, 0);
    for (i = 0; i < m->nbuttons; i++) {
        GtkWidget *b = gtk_button_new_with_label(m->buttons[i].label);
        g_object_set_data_full(G_OBJECT(b), "kt-id", g_strdup(m->buttons[i].id), g_free);
        g_signal_connect(b, "clicked", G_CALLBACK(on_dialog_button), d);
        gtk_box_pack_start(GTK_BOX(row), b, FALSE, FALSE, 0);
    }
    g_signal_connect(close, "clicked", G_CALLBACK(on_close_clicked), d->win);
    gtk_box_pack_end(GTK_BOX(row), close, FALSE, FALSE, 0);
    gtk_box_pack_start(GTK_BOX(box), row, FALSE, FALSE, 0);
    return box;
}

static void v_show_dialog(void *ctx, const kt_dialog *m)
{
    gdlg *d;
    GtkWidget *old;
    (void)ctx;
    for (d = g_dialogs; d; d = d->next)
        if (strcmp(d->kind, m->kind) == 0) break;
    if (!d) {
        d = g_new0(gdlg, 1);
        d->kind = g_strdup(m->kind);
        d->win = gtk_window_new(GTK_WINDOW_TOPLEVEL);
        gtk_window_set_default_size(GTK_WINDOW(d->win), 680, 460);
        g_signal_connect(d->win, "destroy", G_CALLBACK(on_dialog_destroy), d);
        d->next = g_dialogs;
        g_dialogs = d;
    }
    gtk_window_set_title(GTK_WINDOW(d->win), m->title);
    old = gtk_bin_get_child(GTK_BIN(d->win));
    if (old) gtk_widget_destroy(old);        /* update: rebuild the content, keep the window */
    gtk_container_add(GTK_CONTAINER(d->win), make_content(d, m));
    gtk_widget_show_all(d->win);
    gtk_window_present(GTK_WINDOW(d->win));
}

/* ---------------------------------------------------------------- create / destroy */

int gtk_view_create(kt_view *out)
{
    build_icons();
    create_tray_icon();
    memset(out, 0, sizeof *out);
    out->key_store_label = "Open keyring (seahorse)";
    out->bind = v_bind;
    out->claim_single_instance = v_claim_single_instance;
    out->set_state = v_set_state;
    out->set_menu = v_set_menu;
    out->show_dialog = v_show_dialog;
    out->notify = v_notify;
    out->open_key_store = v_open_key_store;
    out->post_to_ui = v_post_to_ui;
    out->run = v_run;
    out->quit = v_quit;
    return 0;
}

void gtk_view_destroy(void)
{
    int i;
    g_events = NULL;   /* the presenter is already freed: no callbacks while closing */
    while (g_dialogs) gtk_widget_destroy(g_dialogs->win);   /* the destroy handler unlinks it */
#ifdef KT_HAVE_XAPP
    if (g_xicon) { xapp_status_icon_set_visible(g_xicon, FALSE); g_object_unref(g_xicon); g_xicon = NULL; }
#endif
    if (g_sicon) { gtk_status_icon_set_visible(g_sicon, FALSE); g_object_unref(g_sicon); g_sicon = NULL; }
    if (g_menu) { g_object_unref(g_menu); g_menu = NULL; }
    for (i = 0; i < 4; i++) {
        if (g_pix[i]) g_object_unref(g_pix[i]);
        g_pix[i] = NULL;
        g_free(g_png[i]);
        g_png[i] = NULL;
    }
    if (g_lock_fd >= 0) { close(g_lock_fd); g_lock_fd = -1; }
}
