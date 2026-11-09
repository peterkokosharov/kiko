/* SPDX-License-Identifier: MIT
 * Author: Peter Kokosharov <peter.kokosharov@gmail.com>
 * win_internal.h - shared declarations of the Win32 GUI layer (one tray instance per process).
 */
#ifndef WIN_INTERNAL_H
#define WIN_INTERNAL_H

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <winsock2.h>
#include <windows.h>

#include "kt_presenter.h"
#include "kt_view.h"

#ifndef WM_DPICHANGED
#define WM_DPICHANGED 0x02E0   /* older SDK headers */
#endif

/* UTF-8 <-> UTF-16 helpers.  Results are malloc'd; NULL on failure. */
wchar_t *kt_win_widen(const char *utf8);
wchar_t *kt_win_widen_crlf(const char *utf8);   /* also turns every LF into CR LF (for EDIT controls) */
char    *kt_win_narrow(const wchar_t *wide);

/* win_view.c: the tray icon, hidden window, menu and message loop. */
int  win_view_create(HINSTANCE hinst, kt_view *out);   /* 0 = ok */
void win_view_destroy(void);

/* win_dialogs.c: native dialog windows. */
int  wd_init(HINSTANCE hinst);
void wd_set_events(const kt_events *events);
void wd_show(const kt_dialog *d);                       /* create, or update + re-focus */
void wd_close_all(void);
int  wd_is_dialog_message(MSG *msg);                    /* 1 = consumed (keyboard navigation) */
UINT wd_dpi_for_window(HWND hwnd);

/* win_host.c: start/stop kiko as a child process. */
void win_host_init(const wchar_t *command_line);
int  win_host_service(void *ctx, kt_action action, char *msg, size_t msgsz);
void win_host_shutdown(void);

#endif
