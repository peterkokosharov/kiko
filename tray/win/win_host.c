/* SPDX-License-Identifier: MIT
 * Author: Peter Kokosharov <peter.kokosharov@gmail.com>
 *
 * win_host.c - starts and stops kiko as a child of the tray.  Windows has no service for kiko-lite,
 * so "Start" runs the configured command (env KIKO_TRAY_COMMAND) without a console window and keeps
 * the process handle.  "Stop" can only stop a process this tray started.
 * These functions block (up to 5 s on stop): the presenter calls them on a worker thread.
 */
#include "win_internal.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

static wchar_t *g_command;       /* malloc'd command line */
static HANDLE   g_proc;          /* child started by the tray, or NULL */

/* Looks for name(.exe) in each directory of %PATH%, one directory at a time.  Empty entries and "."
 * (both mean the current directory) are skipped, and relative entries are ignored: a program in the
 * current directory is never picked unless the command itself contains a path. */
static int find_in_path(const wchar_t *name, wchar_t *full)
{
    DWORD need = GetEnvironmentVariableW(L"PATH", NULL, 0);
    wchar_t *path, *dir, *next;
    int found = 0;

    if (need == 0) return 0;
    path = (wchar_t *)malloc(need * sizeof(wchar_t));
    if (!path) return 0;
    { DWORD got = GetEnvironmentVariableW(L"PATH", path, need);   /* success: 0 < got < need */
      if (got == 0 || got >= need) { free(path); return 0; } }
    for (dir = path; dir && *dir && !found; dir = next) {
        next = wcschr(dir, L';');
        if (next) *next++ = L'\0';
        if (dir[0] == L'\0' || wcscmp(dir, L".") == 0) continue;
        if (dir[0] == L'"') {   /* quoted entry: strip the quotes */
            size_t len = wcslen(dir);
            dir++;
            if (len >= 2 && dir[len - 2] == L'"') dir[len - 2] = L'\0';
        }
        /* absolute only: "X:" + separator, or UNC; "C:" and "C:foo" are drive-relative and refused */
        if (!((dir[0] && dir[1] == L':' && (dir[2] == 0x5C || dir[2] == L'/')) || (dir[0] == 0x5C && dir[1] == 0x5C))) continue;
        { DWORD r = SearchPathW(dir, name, L".exe", MAX_PATH, full, NULL);
          if (r > 0 && r < MAX_PATH) found = 1; }   /* r >= MAX_PATH: buffer too small, not a hit */
    }
    free(path);
    return found;
}

/* Resolves the program of the command line to a full path, so CreateProcessW never searches the
 * current directory (a planted kiko-lite.exe).  Returns a malloc'd command line with the program
 * quoted, or NULL if the program cannot be found. */
static wchar_t *resolve_command(const wchar_t *cmd)
{
    wchar_t name[MAX_PATH], full[MAX_PATH];
    const wchar_t *p = cmd, *rest;
    size_t n = 0, i;
    wchar_t *out;
    int has_sep = 0;

    full[0] = 0;

    while (*p == L' ') p++;
    if (*p == L'"') {
        p++;
        while (*p && *p != L'"' && n < MAX_PATH - 1) name[n++] = *p++;
        if (*p == L'"') p++;
    } else {
        while (*p && *p != L' ' && n < MAX_PATH - 1) name[n++] = *p++;
    }
    name[n] = L'\0';
    rest = p;
    if (n == 0) return NULL;
    for (i = 0; i < n; i++)
        if (name[i] == 0x5C || name[i] == L'/' || name[i] == L':') has_sep = 1;   /* 0x5C = backslash */
    if (has_sep) {
        DWORD r = GetFullPathNameW(name, MAX_PATH, full, NULL);
        if (r == 0 || r >= MAX_PATH) return NULL;   /* r >= MAX_PATH: buffer too small */
    } else {
        /* explicit PATH search; SearchPathW(NULL, ...) would look in the current directory early */
        if (!find_in_path(name, full)) return NULL;
    }
    out = (wchar_t *)malloc((wcslen(full) + wcslen(rest) + 4) * sizeof(wchar_t));
    if (!out) return NULL;
    swprintf(out, wcslen(full) + wcslen(rest) + 4, L"\"%ls\"%ls", full, rest);
    return out;
}

void win_host_init(const wchar_t *command_line)
{
    SetSearchPathMode(BASE_SEARCH_PATH_ENABLE_SAFE_SEARCHMODE | BASE_SEARCH_PATH_PERMANENT);
    free(g_command);
    g_command = (wchar_t *)malloc((wcslen(command_line) + 1) * sizeof(wchar_t));
    if (g_command) wcscpy(g_command, command_line);
}

static int child_alive(void)
{
    if (!g_proc) return 0;
    if (WaitForSingleObject(g_proc, 0) == WAIT_TIMEOUT) return 1;
    CloseHandle(g_proc);
    g_proc = NULL;
    return 0;
}

static int do_start(char *msg, size_t msgsz)
{
    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    wchar_t *cmd;
    BOOL ok;

    if (child_alive()) {
        snprintf(msg, msgsz, "kiko was already started by the tray");
        return 0;
    }
    if (!g_command || !g_command[0]) {
        snprintf(msg, msgsz, "No start command configured (KIKO_TRAY_COMMAND)");
        return -1;
    }
    cmd = resolve_command(g_command);   /* full path, never the current directory */
    if (!cmd) { snprintf(msg, msgsz, "Cannot find the kiko program (check KIKO_TRAY_COMMAND)"); return -1; }
    memset(&si, 0, sizeof si);
    si.cb = sizeof si;
    memset(&pi, 0, sizeof pi);
    ok = CreateProcessW(NULL, cmd, NULL, NULL, FALSE, CREATE_NO_WINDOW, NULL, NULL, &si, &pi);
    free(cmd);
    if (!ok) {
        snprintf(msg, msgsz, "Could not start kiko (Windows error %lu). Check KIKO_TRAY_COMMAND.",
                 (unsigned long)GetLastError());
        return -1;
    }
    CloseHandle(pi.hThread);
    g_proc = pi.hProcess;
    snprintf(msg, msgsz, "kiko started");
    return 0;
}

static int do_stop(char *msg, size_t msgsz)
{
    if (!child_alive()) {
        snprintf(msg, msgsz, "kiko was not started by the tray; stop it where it was started");
        return -1;
    }
    TerminateProcess(g_proc, 1);
    WaitForSingleObject(g_proc, 5000);
    CloseHandle(g_proc);
    g_proc = NULL;
    snprintf(msg, msgsz, "kiko stopped");
    return 0;
}

int win_host_service(void *ctx, kt_action action, char *msg, size_t msgsz)
{
    (void)ctx;
    msg[0] = '\0';
    switch (action) {
    case KT_ACT_START:
        return do_start(msg, msgsz);
    case KT_ACT_STOP:
        return do_stop(msg, msgsz);
    case KT_ACT_RESTART:
        if (do_stop(msg, msgsz) != 0) return -1;
        return do_start(msg, msgsz);
    }
    return -1;
}

void win_host_shutdown(void)
{
    if (g_proc) { CloseHandle(g_proc); g_proc = NULL; }   /* the child keeps running; only our handle goes */
    free(g_command);
    g_command = NULL;
}
