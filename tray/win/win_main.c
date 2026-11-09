/* SPDX-License-Identifier: MIT
 * Author: Peter Kokosharov <peter.kokosharov@gmail.com>
 *
 * win_main.c - kiko-tray for Windows: reads the configuration, builds the Win32 view and the
 * presenter, runs the message loop.
 *
 * Environment: KIKO_PORT (default 4000), KIKO_API_TOKEN or the file %USERPROFILE%\.kiko\api-token,
 * KIKO_TRAY_COMMAND (default: kiko-lite serve --no-auth --host 127.0.0.1).
 */
#include "win_internal.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

#include "kt_util.h"

#ifndef KT_VERSION
#define KT_VERSION "0.1.0"
#endif

#define DEFAULT_COMMAND L"kiko-lite serve --no-auth --host 127.0.0.1"

/* Reads the first line of the token file; returns a malloc'd string or NULL.  The value is never logged. */
static char *read_token_file(void)
{
    wchar_t path[MAX_PATH + 32];
    wchar_t *home = _wgetenv(L"USERPROFILE");
    FILE *f;
    char line[256];
    size_t n;

    if (!home || wcslen(home) > MAX_PATH) return NULL;
    swprintf(path, sizeof path / sizeof path[0], L"%ls\\.kiko\\api-token", home);
    f = _wfopen(path, L"rb");
    if (!f) return NULL;
    if (!fgets(line, sizeof line, f)) { fclose(f); return NULL; }
    fclose(f);
    n = strlen(line);
    while (n > 0 && (line[n - 1] == '\n' || line[n - 1] == '\r' || line[n - 1] == ' ')) line[--n] = '\0';
    return n ? kt_strdup(line) : NULL;
}

int WINAPI WinMain(HINSTANCE hinst, HINSTANCE hprev, LPSTR cmdline, int show)
{
    kt_view view;
    kt_host host;
    kt_config cfg;
    kt_presenter *p;
    const wchar_t *wport = _wgetenv(L"KIKO_PORT");
    const wchar_t *wcmd = _wgetenv(L"KIKO_TRAY_COMMAND");
    const wchar_t *wtok = _wgetenv(L"KIKO_API_TOKEN");
    char *token = NULL, *cmd_utf8;
    const char *token_source = "none";
    int rc = 0;
    int port = 4000;

    (void)hprev; (void)cmdline; (void)show;
    kt_net_startup();

    if (wport && _wtoi(wport) > 0 && _wtoi(wport) < 65536) port = _wtoi(wport);
    if (wtok && wtok[0]) {
        token = kt_win_narrow(wtok);
        token_source = "env KIKO_API_TOKEN";
    } else {
        token = read_token_file();
        if (token) token_source = "file %USERPROFILE%\\.kiko\\api-token";
    }
    if (!wcmd || !wcmd[0]) wcmd = DEFAULT_COMMAND;
    win_host_init(wcmd);
    cmd_utf8 = kt_win_narrow(wcmd);

    if (win_view_create(hinst, &view) != 0) {
        MessageBoxW(NULL, L"kiko-tray could not create its window.", L"kiko-tray", MB_ICONERROR | MB_OK);
        rc = 1;
        goto done;
    }
    if (!view.claim_single_instance(view.ctx)) {
        MessageBoxW(NULL, L"kiko-tray is already running (look for its icon near the clock).", L"kiko-tray",
                    MB_ICONINFORMATION | MB_OK);
        win_view_destroy();
        goto done;
    }

    memset(&cfg, 0, sizeof cfg);
    cfg.port = port;
    cfg.api_token = token;
    cfg.token_source = token_source;
    cfg.command_text = cmd_utf8;
    cfg.version = KT_VERSION;
    cfg.poll_interval_ms = 5000;
    cfg.start_grace_ms = 30000;
    host.ctx = NULL;
    host.service = win_host_service;

    p = kt_presenter_new(&view, &host, &cfg);
    if (!p || kt_presenter_start(p) != 0) {
        MessageBoxW(NULL, L"kiko-tray could not start.", L"kiko-tray", MB_ICONERROR | MB_OK);
        kt_presenter_free(p);
        win_view_destroy();
        rc = 1;
        goto done;
    }
    view.run(view.ctx);          /* returns after Quit */
    kt_presenter_stop(p);
    kt_presenter_free(p);
    win_view_destroy();          /* the tray icon is gone before the process exits */

done:
    win_host_shutdown();
    free(cmd_utf8);
    if (token) {
        kt_secure_zero(token, strlen(token));   /* do not leave the token lying around */
        free(token);
    }
    return rc;
}
