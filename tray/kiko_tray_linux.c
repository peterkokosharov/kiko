/*
 * Author: Peter Kokosharov <peter.kokosharov@gmail.com>
 * AI models: claude-sonnet-4-6
 */

/*
 * kiko_tray_linux.c — GTK3 system tray icon for KIKO (Linux/TDE)
 *
 * Compile:
 *   gcc kiko_tray_linux.c -o kiko-tray \
 *       $(pkg-config --cflags --libs gtk+-3.0) \
 *       -lm -Wall -O2
 *
 * Deps (Debian/Mint):
 *   sudo apt install libgtk-3-dev pkg-config build-essential
 *
 * TDE note: uses GtkStatusIcon (XEmbed) — works with Trinity kicker.
 */

#include <gtk/gtk.h>
#include <cairo.h>
#include <signal.h>
#include <sys/wait.h>
#include <sys/stat.h>
#include <unistd.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>

static GPid          g_kiko_pid  = 0;
static GtkStatusIcon *g_icon     = NULL;

static void update_tooltip(void);

static void get_config_path(char *buf, size_t sz) {
    const char *env = getenv("KIKO_CONFIG");
    if (env) { snprintf(buf, sz, "%s", env); return; }
    const char *home = getenv("HOME");
    snprintf(buf, sz, "%s/.config/kiko/kiko.yaml", home ? home : "/tmp");
}

static gboolean config_exists(void) {
    char path[4096];
    get_config_path(path, sizeof(path));
    struct stat st;
    return stat(path, &st) == 0;
}

static gboolean is_running(void) {
    if (g_kiko_pid <= 0) return FALSE;
    if (waitpid(g_kiko_pid, NULL, WNOHANG) != 0) {
        g_kiko_pid = 0;
        return FALSE;
    }
    return TRUE;
}

static void start_kiko(void) {
    if (is_running()) return;
    if (!config_exists()) {
        GtkWidget *dlg = gtk_message_dialog_new(
            NULL, GTK_DIALOG_MODAL, GTK_MESSAGE_WARNING, GTK_BUTTONS_OK,
            "Config file not found.\nCreate ~/.config/kiko/kiko.yaml first.");
        gtk_dialog_run(GTK_DIALOG(dlg));
        gtk_widget_destroy(dlg);
        return;
    }
    char config[4096];
    get_config_path(config, sizeof(config));
    char *argv[] = { "kiko", "--config", config, "--port", "4000", NULL };
    GError *err = NULL;
    if (!g_spawn_async(NULL, argv, NULL,
                       G_SPAWN_SEARCH_PATH | G_SPAWN_DO_NOT_REAP_CHILD,
                       NULL, NULL, &g_kiko_pid, &err)) {
        GtkWidget *dlg = gtk_message_dialog_new(
            NULL, GTK_DIALOG_MODAL, GTK_MESSAGE_ERROR, GTK_BUTTONS_OK,
            "Failed to start kiko:\n%s", err ? err->message : "unknown");
        gtk_dialog_run(GTK_DIALOG(dlg));
        gtk_widget_destroy(dlg);
        g_clear_error(&err);
        return;
    }
    g_child_watch_add(g_kiko_pid, (GChildWatchFunc)g_spawn_close_pid, NULL);
    update_tooltip();
}

static void stop_kiko(void) {
    if (!is_running()) return;
    kill(g_kiko_pid, SIGTERM);
    for (int i = 0; i < 30; i++) {
        usleep(100000);
        if (!is_running()) goto done;
    }
    kill(g_kiko_pid, SIGKILL);
    waitpid(g_kiko_pid, NULL, 0);
done:
    g_kiko_pid = 0;
    update_tooltip();
}

static void open_config(void) {
    char path[4096];
    get_config_path(path, sizeof(path));
    char cmd[4300];
    snprintf(cmd, sizeof(cmd), "xdg-open '%s' 2>/dev/null || gedit '%s' &", path, path);
    system(cmd);
}

static void update_tooltip(void) {
    if (!g_icon) return;
    gtk_status_icon_set_tooltip_text(g_icon,
        is_running() ? "KIKO  \xe2\x97\x8f Running" : "KIKO  \xe2\x97\x8b Stopped");
}

static GdkPixbuf *make_icon(int size) {
    cairo_surface_t *surf = cairo_image_surface_create(CAIRO_FORMAT_ARGB32, size, size);
    cairo_t *cr = cairo_create(surf);
    cairo_set_source_rgb(cr, 0.12, 0.12, 0.12);
    cairo_rectangle(cr, 0, 0, size, size);
    cairo_fill(cr);
    cairo_set_source_rgb(cr, 1.0, 1.0, 1.0);
    cairo_select_font_face(cr, "Sans", CAIRO_FONT_SLANT_NORMAL, CAIRO_FONT_WEIGHT_BOLD);
    cairo_set_font_size(cr, size * 0.65);
    cairo_text_extents_t te;
    cairo_text_extents(cr, "K", &te);
    cairo_move_to(cr,
        (size - te.width)  / 2.0 - te.x_bearing,
        (size - te.height) / 2.0 - te.y_bearing);
    cairo_show_text(cr, "K");
    cairo_destroy(cr);
    GdkPixbuf *pb = gdk_pixbuf_get_from_surface(surf, 0, 0, size, size);
    cairo_surface_destroy(surf);
    return pb;
}

static void on_start(GtkMenuItem *i, gpointer d)  { (void)i;(void)d; start_kiko(); }
static void on_stop(GtkMenuItem *i, gpointer d)   { (void)i;(void)d; stop_kiko();  }
static void on_config(GtkMenuItem *i, gpointer d) { (void)i;(void)d; open_config(); }
static void on_exit(GtkMenuItem *i, gpointer d)   { (void)i;(void)d; stop_kiko(); gtk_main_quit(); }

static void show_menu(GtkStatusIcon *icon, guint button, guint activate_time, gpointer data) {
    (void)data;
    gboolean run = is_running(), has_cfg = config_exists();
    GtkWidget *menu = gtk_menu_new();

    GtkWidget *status = gtk_menu_item_new_with_label(run ? "\xe2\x97\x8f Running" : "\xe2\x97\x8b Stopped");
    gtk_widget_set_sensitive(status, FALSE);
    gtk_menu_shell_append(GTK_MENU_SHELL(menu), status);
    gtk_menu_shell_append(GTK_MENU_SHELL(menu), gtk_separator_menu_item_new());

    GtkWidget *w_start = gtk_menu_item_new_with_label("Start KIKO");
    gtk_widget_set_sensitive(w_start, !run && has_cfg);
    g_signal_connect(w_start, "activate", G_CALLBACK(on_start), NULL);
    gtk_menu_shell_append(GTK_MENU_SHELL(menu), w_start);

    GtkWidget *w_stop = gtk_menu_item_new_with_label("Stop KIKO");
    gtk_widget_set_sensitive(w_stop, run);
    g_signal_connect(w_stop, "activate", G_CALLBACK(on_stop), NULL);
    gtk_menu_shell_append(GTK_MENU_SHELL(menu), w_stop);

    gtk_menu_shell_append(GTK_MENU_SHELL(menu), gtk_separator_menu_item_new());

    char cfg_label[80];
    snprintf(cfg_label, sizeof(cfg_label), "Open config%s", has_cfg ? "" : "  (missing)");
    GtkWidget *w_cfg = gtk_menu_item_new_with_label(cfg_label);
    gtk_widget_set_sensitive(w_cfg, has_cfg);
    g_signal_connect(w_cfg, "activate", G_CALLBACK(on_config), NULL);
    gtk_menu_shell_append(GTK_MENU_SHELL(menu), w_cfg);

    gtk_menu_shell_append(GTK_MENU_SHELL(menu), gtk_separator_menu_item_new());

    GtkWidget *w_exit = gtk_menu_item_new_with_label("Exit");
    g_signal_connect(w_exit, "activate", G_CALLBACK(on_exit), NULL);
    gtk_menu_shell_append(GTK_MENU_SHELL(menu), w_exit);

    gtk_widget_show_all(menu);
    gtk_menu_popup(GTK_MENU(menu), NULL, NULL,
                   gtk_status_icon_position_menu, icon, button, activate_time);
}

static gboolean poll_status(gpointer data) { (void)data; update_tooltip(); return G_SOURCE_CONTINUE; }

int main(int argc, char **argv) {
    gtk_init(&argc, &argv);
    GdkPixbuf *pb = make_icon(22);
    g_icon = gtk_status_icon_new_from_pixbuf(pb);
    g_object_unref(pb);
    gtk_status_icon_set_visible(g_icon, TRUE);
    update_tooltip();
    g_signal_connect(g_icon, "popup-menu", G_CALLBACK(show_menu), NULL);
    g_signal_connect(g_icon, "activate",   G_CALLBACK(on_config), NULL);
    g_timeout_add_seconds(5, poll_status, NULL);
    gtk_main();
    return 0;
}
