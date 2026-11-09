/* SPDX-License-Identifier: MIT
 * Author: Peter Kokosharov <peter.kokosharov@gmail.com>
 *
 * gtk_main.c - kiko-tray for Linux Mint / GTK3.
 * Environment: KIKO_PORT (default 4000), KIKO_API_TOKEN or the file ~/.kiko/api-token.
 * kiko is controlled through `systemctl --user ... kiko-lite` (tools/kiko-lite.service in the kiko repo).
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "gtk_internal.h"
#include "kt_util.h"

#ifndef KT_VERSION
#define KT_VERSION "0.1.0"
#endif

/* First line of ~/.kiko/api-token, or NULL.  The value is never logged. */
static char *read_token_file(void)
{
    gchar *path = g_build_filename(g_get_home_dir(), ".kiko", "api-token", NULL);
    FILE *f = fopen(path, "rb");
    char line[256];
    size_t n;
    g_free(path);
    if (!f) return NULL;
    if (!fgets(line, sizeof line, f)) { fclose(f); return NULL; }
    fclose(f);
    n = strlen(line);
    while (n > 0 && (line[n - 1] == '\n' || line[n - 1] == '\r' || line[n - 1] == ' ')) line[--n] = '\0';
    return n ? kt_strdup(line) : NULL;
}

int main(int argc, char **argv)
{
    kt_view view;
    kt_host host;
    kt_config cfg;
    kt_presenter *p;
    const char *port_s, *tok_env;
    char *token = NULL;
    const char *token_source = "none";
    int port = 4000;

    gtk_init(&argc, &argv);
    kt_net_startup();

    port_s = g_getenv("KIKO_PORT");
    if (port_s && atoi(port_s) > 0 && atoi(port_s) < 65536) port = atoi(port_s);
    tok_env = g_getenv("KIKO_API_TOKEN");
    if (tok_env && tok_env[0]) {
        token = kt_strdup(tok_env);
        token_source = "env KIKO_API_TOKEN";
    } else {
        token = read_token_file();
        if (token) token_source = "file ~/.kiko/api-token";
    }

    if (gtk_view_create(&view) != 0) return 1;
    if (!view.claim_single_instance(view.ctx)) {
        fprintf(stderr, "kiko-tray is already running\n");
        gtk_view_destroy();
        return 0;
    }
    memset(&cfg, 0, sizeof cfg);
    cfg.port = port;
    cfg.api_token = token;
    cfg.token_source = token_source;
    cfg.command_text = "systemctl --user start|stop|restart kiko-lite";
    cfg.version = KT_VERSION;
    cfg.poll_interval_ms = 5000;
    cfg.start_grace_ms = 30000;
    host.ctx = NULL;
    host.service = gtk_host_service;

    p = kt_presenter_new(&view, &host, &cfg);
    if (!p || kt_presenter_start(p) != 0) {
        fprintf(stderr, "kiko-tray: could not start\n");
        kt_presenter_free(p);
        gtk_view_destroy();
        return 1;
    }
    view.run(view.ctx);
    kt_presenter_stop(p);
    kt_presenter_free(p);
    gtk_view_destroy();
    if (token) { kt_secure_zero(token, strlen(token)); free(token); }
    return 0;
}
