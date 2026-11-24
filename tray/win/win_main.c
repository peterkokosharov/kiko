/* SPDX-License-Identifier: MIT
 * Author: Peter Kokosharov <peter.kokosharov@gmail.com>
 * AI models: claude-sonnet-5-5
 *
 * win_main.c - kiko-tray for Windows: reads the configuration, builds the Win32 view and the
 * presenter, runs the message loop.
 *
 * Configuration, strongest first:
 *   environment: KIKO_TRAY_COMMAND, KIKO_PORT, KIKO_API_TOKEN
 *   kiko-tray.ini next to the exe: command, port, token_file (%VAR% is expanded in token_file)
 *   defaults: port 4000, token file %USERPROFILE%\.kiko\api-token, NO start command (Start explains).
 */
#include "win_internal.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

#include "kt_ini.h"
#include "kt_util.h"

#ifndef KT_VERSION
#define KT_VERSION "0.1.0"
#endif

#define INI_MAX (64 * 1024)

/* The text of kiko-tray.ini (next to the exe) as a malloc'd string, or NULL when there is none. */
static char *read_ini(void)
{
    wchar_t path[MAX_PATH + 16];
    DWORD n = GetModuleFileNameW(NULL, path, MAX_PATH);
    wchar_t *slash;
    FILE *f;
    char *buf;
    size_t got;

    if (n == 0 || n >= MAX_PATH) return NULL;
    slash = wcsrchr(path, L'\\');
    if (!slash) return NULL;
    wcscpy(slash + 1, L"kiko-tray.ini");
    f = _wfopen(path, L"rb");
    if (!f) return NULL;
    buf = (char *)malloc(INI_MAX + 1);
    if (!buf) { fclose(f); return NULL; }
    got = fread(buf, 1, INI_MAX, f);
    fclose(f);
    buf[got] = '\0';
    return buf;
}

/* kt_expand_vars callback: %NAME% from the environment (UTF-8 in, UTF-8 out, one static buffer). */
static const char *env_lookup(const char *name, void *ctx)
{
    static char out[2048];
    wchar_t *wname = kt_win_widen(name);
    wchar_t wval[1024];
    DWORD got;
    int n;
    (void)ctx;
    if (!wname) return NULL;
    got = GetEnvironmentVariableW(wname, wval, 1024);
    free(wname);
    if (got == 0 || got >= 1024) return NULL;
    n = WideCharToMultiByte(CP_UTF8, 0, wval, -1, out, (int)sizeof out, NULL, NULL);
    return n > 0 ? out : NULL;
}

/* Reads the first line of the token file; returns a malloc'd string or NULL.  The value is never logged. */
static char *read_token_file(const wchar_t *path)
{
    FILE *f;
    char line[256];
    size_t n;

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
    char *ini = read_ini();
    char *ini_cmd = NULL, *ini_port = NULL, *ini_tok = NULL;
    wchar_t *cmd_w = NULL;
    char *token = NULL, *cmd_utf8 = NULL;
    char token_source_buf[MAX_PATH * 3 + 32];
    const char *token_source = "none";
    int rc = 0;
    int port = 4000;

    (void)hprev; (void)cmdline; (void)show;
    kt_net_startup();

    if (ini) {
        ini_cmd = kt_ini_get(ini, "command");
        ini_port = kt_ini_get(ini, "port");
        ini_tok = kt_ini_get(ini, "token_file");
    }

    /* port: env, then ini, else 4000 */
    if (wport && _wtoi(wport) > 0 && _wtoi(wport) < 65536) port = _wtoi(wport);
    else if (ini_port && atoi(ini_port) > 0 && atoi(ini_port) < 65536) port = atoi(ini_port);

    /* token: env, then the ini token_file, then %USERPROFILE%\.kiko\api-token */
    if (wtok && wtok[0]) {
        token = kt_win_narrow(wtok);
        token_source = "env KIKO_API_TOKEN";
    } else {
        if (ini_tok && ini_tok[0]) {
            char *expanded = kt_expand_vars(ini_tok, env_lookup, NULL);
            wchar_t *wpath = expanded ? kt_win_widen(expanded) : NULL;
            if (wpath) {
                token = read_token_file(wpath);
                if (token) {
                    snprintf(token_source_buf, sizeof token_source_buf, "file %s", expanded);
                    token_source = token_source_buf;
                }
            }
            free(wpath);
            free(expanded);
        }
        if (!token) {
            wchar_t path[MAX_PATH + 32];
            wchar_t *home = _wgetenv(L"USERPROFILE");
            if (home && wcslen(home) <= MAX_PATH) {
                swprintf(path, sizeof path / sizeof path[0], L"%ls\\.kiko\\api-token", home);
                token = read_token_file(path);
                if (token) token_source = "file %USERPROFILE%\\.kiko\\api-token";
            }
        }
    }

    /* start command: env, then ini; never a default (a default must not switch authentication off) */
    if (wcmd && wcmd[0]) {
        cmd_w = (wchar_t *)malloc((wcslen(wcmd) + 1) * sizeof(wchar_t));
        if (cmd_w) wcscpy(cmd_w, wcmd);
    } else if (ini_cmd && ini_cmd[0]) {
        cmd_w = kt_win_widen(ini_cmd);
    }
    win_host_init(cmd_w);   /* NULL = not configured */
    cmd_utf8 = cmd_w ? kt_win_narrow(cmd_w) : NULL;

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
    cfg.command_text = cmd_utf8;     /* NULL shows "(not configured)" */
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
    free(cmd_w);
    free(ini_cmd);
    free(ini_port);
    free(ini_tok);
    free(ini);
    if (token) {
        kt_secure_zero(token, strlen(token));   /* do not leave the token lying around */
        free(token);
    }
    return rc;
}
