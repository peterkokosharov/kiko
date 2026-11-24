/* SPDX-License-Identifier: MIT
 * Author: Peter Kokosharov <peter.kokosharov@gmail.com>
 * AI models: nvidia-nemotron-3-ultra, claude-sonnet-5-5
 *
 * gtk_host.c - start/stop/restart kiko through the systemd USER unit (default kiko-lite.service, override with
 * the env var KIKO_UNIT; created by `kiko install` or by the zip's install-service.sh).
 * Fixed argument vector, no shell.
 */
#include "gtk_internal.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int gtk_host_service(void *ctx, kt_action action, char *msg, size_t msgsz)
{
    const char *verb = action == KT_ACT_START ? "start" : (action == KT_ACT_STOP ? "stop" : "restart");
    char *argv[5];
    gchar *std_err = NULL;
    gint status = 0;
    GError *err = NULL;
    int rc = -1;
    (void)ctx;

    argv[0] = (char *)"systemctl";
    argv[1] = (char *)"--user";
    argv[2] = (char *)verb;
    const char *unit = getenv("KIKO_UNIT");
    if (!unit || !unit[0]) unit = "kiko-lite";
    argv[3] = (char *)unit;
    argv[4] = NULL;

    if (!g_spawn_sync(NULL, argv, NULL, G_SPAWN_SEARCH_PATH | G_SPAWN_STDOUT_TO_DEV_NULL, NULL, NULL, NULL,
                      &std_err, &status, &err)) {
        snprintf(msg, msgsz, "Cannot run systemctl: %s", err ? err->message : "unknown error");
    } else if (g_spawn_check_exit_status(status, NULL)) {
        snprintf(msg, msgsz, "kiko-lite: %s done", verb);
        rc = 0;
    } else {
        char *nl = std_err ? strchr(std_err, '\n') : NULL;
        if (nl) *nl = '\0';
        snprintf(msg, msgsz, "systemctl --user %s %s failed: %s", verb, unit,
                 (std_err && std_err[0]) ? std_err : "see journalctl --user -u <unit>");
    }
    g_clear_error(&err);
    g_free(std_err);
    return rc;
}
