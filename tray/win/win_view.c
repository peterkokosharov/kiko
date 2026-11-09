/* SPDX-License-Identifier: MIT
 * Author: Peter Kokosharov <peter.kokosharov@gmail.com>
 *
 * win_view.c - the Win32 tray: hidden message window, Shell_NotifyIconW icon, popup menu,
 * balloon notifications, in-memory state icons, single-instance mutex, UI-thread post queue.
 * All entry points except post_to_ui and quit run on the UI thread (the one that created the window).
 */
#include "win_internal.h"

#include <objbase.h>
#include <shellapi.h>
#include <windowsx.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

#include "kt_icon.h"
#include "kt_thread.h"
#include "kt_util.h"

#define WM_KT_TRAY   (WM_APP + 1)   /* tray icon callback */
#define WM_KT_POST   (WM_APP + 2)   /* "run the posted functions" */
#define ID_MENU_BASE 1000
#define TRAY_UID     1

typedef struct { wchar_t *label; char *id; int enabled; int separator; } menu_entry;
typedef struct post_node { kt_ui_fn fn; void *arg; struct post_node *next; } post_node;

static HINSTANCE        g_hinst;
static HWND             g_hwnd;
static UINT             g_wm_taskbar;
static HICON            g_icons[4];
static kt_state         g_state = KT_STATE_STARTING;
static wchar_t          g_tip[128] = L"KIKO";
static int              g_icon_added;
static const kt_events *g_events;
static menu_entry      *g_menu;
static int              g_menu_n;
static HANDLE           g_single_mutex;
static kt_mutex         g_post_mu;
static post_node       *g_post_head, *g_post_tail;
static int              g_destroyed;
static int              g_com_init;
static int              g_v4;   /* NIM_SETVERSION to NOTIFYICON_VERSION_4 succeeded */

/* ---------------------------------------------------------------- UTF-8 helpers */

wchar_t *kt_win_widen(const char *s)
{
    int n;
    wchar_t *w;
    if (!s) return NULL;
    n = MultiByteToWideChar(CP_UTF8, 0, s, -1, NULL, 0);
    if (n <= 0) return NULL;
    w = (wchar_t *)malloc((size_t)n * sizeof *w);
    if (w && MultiByteToWideChar(CP_UTF8, 0, s, -1, w, n) <= 0) { free(w); return NULL; }
    return w;
}

wchar_t *kt_win_widen_crlf(const char *s)
{
    kt_sb sb;
    const char *p;
    char *fixed;
    wchar_t *w;
    if (!s) return NULL;
    kt_sb_init(&sb);
    for (p = s; *p; p++) {
        if (*p == '\n' && (p == s || p[-1] != '\r')) kt_sb_append(&sb, "\r\n");
        else kt_sb_appendn(&sb, p, 1);
    }
    fixed = kt_sb_take(&sb);
    w = kt_win_widen(fixed);
    free(fixed);
    return w;
}

char *kt_win_narrow(const wchar_t *w)
{
    int n;
    char *s;
    if (!w) return NULL;
    n = WideCharToMultiByte(CP_UTF8, 0, w, -1, NULL, 0, NULL, NULL);
    if (n <= 0) return NULL;
    s = (char *)malloc((size_t)n);
    if (s && WideCharToMultiByte(CP_UTF8, 0, w, -1, s, n, NULL, NULL) <= 0) { free(s); return NULL; }
    return s;
}

/* ---------------------------------------------------------------- state icons (GDI, in memory) */

static HICON make_icon(kt_state st, int size)
{
    BITMAPINFO bi;
    void *bits = NULL;
    HDC dc = GetDC(NULL);
    HBITMAP color, mask;
    ICONINFO ii;
    HICON icon = NULL;
    uint8_t *px = (uint8_t *)malloc((size_t)size * (size_t)size * 4u);

    if (!px) { ReleaseDC(NULL, dc); return NULL; }
    kt_icon_render(size, st, 1, px);   /* straight alpha, BGRA, top-down */
    memset(&bi, 0, sizeof bi);
    bi.bmiHeader.biSize = sizeof bi.bmiHeader;
    bi.bmiHeader.biWidth = size;
    bi.bmiHeader.biHeight = -size;     /* top-down */
    bi.bmiHeader.biPlanes = 1;
    bi.bmiHeader.biBitCount = 32;
    bi.bmiHeader.biCompression = BI_RGB;
    color = CreateDIBSection(dc, &bi, DIB_RGB_COLORS, &bits, NULL, 0);
    mask = CreateBitmap(size, size, 1, 1, NULL);   /* all zero: the alpha channel does the shaping */
    if (color && bits && mask) {
        memcpy(bits, px, (size_t)size * (size_t)size * 4u);
        ii.fIcon = TRUE;
        ii.xHotspot = 0;
        ii.yHotspot = 0;
        ii.hbmMask = mask;
        ii.hbmColor = color;
        icon = CreateIconIndirect(&ii);
    }
    if (color) DeleteObject(color);
    if (mask) DeleteObject(mask);
    ReleaseDC(NULL, dc);
    free(px);
    return icon;
}

static void free_icons(void)
{
    int i;
    for (i = 0; i < 4; i++) {
        if (g_icons[i]) DestroyIcon(g_icons[i]);
        g_icons[i] = NULL;
    }
}

static void build_icons(void)
{
    int size = GetSystemMetrics(SM_CXSMICON);
    int i;
    if (size < 16) size = 16;
    if (size > 64) size = 64;
    free_icons();
    for (i = 0; i < 4; i++) g_icons[i] = make_icon((kt_state)i, size);
}

/* ---------------------------------------------------------------- Shell_NotifyIconW */

static void fill_nid(NOTIFYICONDATAW *n, UINT flags)
{
    memset(n, 0, sizeof *n);
    n->cbSize = sizeof *n;
    n->hWnd = g_hwnd;
    n->uID = TRAY_UID;
    n->uFlags = flags;
}

static void icon_add(void)
{
    NOTIFYICONDATAW n;
    if (!g_hwnd || !g_icons[g_state]) return;
    fill_nid(&n, NIF_MESSAGE | NIF_ICON | NIF_TIP);
    n.uCallbackMessage = WM_KT_TRAY;
    n.hIcon = g_icons[g_state];
    wcsncpy(n.szTip, g_tip, (sizeof n.szTip / sizeof n.szTip[0]) - 1);
    g_icon_added = Shell_NotifyIconW(NIM_ADD, &n) ? 1 : 0;   /* may fail before the taskbar exists */
    g_v4 = 0;
    if (g_icon_added) {
        n.uVersion = NOTIFYICON_VERSION_4;   /* callback: LOWORD(lParam) = event, WM_CONTEXTMENU for the menu */
        g_v4 = Shell_NotifyIconW(NIM_SETVERSION, &n) ? 1 : 0;   /* else: legacy callbacks */
    }
}

static void icon_update(void)
{
    NOTIFYICONDATAW n;
    if (!g_icon_added) { icon_add(); return; }
    fill_nid(&n, NIF_ICON | NIF_TIP);
    n.hIcon = g_icons[g_state];
    wcsncpy(n.szTip, g_tip, (sizeof n.szTip / sizeof n.szTip[0]) - 1);
    if (!Shell_NotifyIconW(NIM_MODIFY, &n)) icon_add();
}

static void icon_remove(void)
{
    NOTIFYICONDATAW n;
    if (!g_icon_added) return;
    fill_nid(&n, 0);
    Shell_NotifyIconW(NIM_DELETE, &n);
    g_icon_added = 0;
}

/* ---------------------------------------------------------------- popup menu */

static void menu_free(void)
{
    int i;
    for (i = 0; i < g_menu_n; i++) {
        free(g_menu[i].label);
        free(g_menu[i].id);
    }
    free(g_menu);
    g_menu = NULL;
    g_menu_n = 0;
}

/* have_xy: v4 gives the anchor point; legacy callbacks do not, so use the cursor position. */
static void show_popup_menu(int have_xy, int ax, int ay)
{
    HMENU m;
    POINT pt;
    int i, n;
    UINT cmd;
    char **ids;    /* snapshot: a menu rebuild during the modal loop (posted set_menu) must not change */
    int *enabled;  /* what the chosen command means */

    if (g_menu_n == 0) return;
    n = g_menu_n;
    ids = (char **)calloc((size_t)n, sizeof *ids);
    enabled = (int *)calloc((size_t)n, sizeof *enabled);
    m = CreatePopupMenu();
    if (!ids || !enabled || !m) {
        if (m) DestroyMenu(m);
        free(ids);
        free(enabled);
        return;
    }
    for (i = 0; i < n; i++) {
        if (g_menu[i].separator) {
            AppendMenuW(m, MF_SEPARATOR, 0, NULL);
        } else {
            AppendMenuW(m, MF_STRING | (g_menu[i].enabled ? MF_ENABLED : MF_GRAYED),
                        (UINT_PTR)(ID_MENU_BASE + i), g_menu[i].label);
            ids[i] = kt_strdup(g_menu[i].id ? g_menu[i].id : "");
            enabled[i] = g_menu[i].enabled;
        }
    }
    if (have_xy) { pt.x = ax; pt.y = ay; } else GetCursorPos(&pt);
    SetForegroundWindow(g_hwnd);   /* without this the menu does not close when the user clicks elsewhere */
    cmd = (UINT)TrackPopupMenu(m, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY | TPM_BOTTOMALIGN,
                               pt.x, pt.y, 0, g_hwnd, NULL);
    PostMessageW(g_hwnd, WM_NULL, 0, 0);
    DestroyMenu(m);
    if (cmd >= ID_MENU_BASE && cmd < (UINT)(ID_MENU_BASE + n)) {
        const char *id = ids[cmd - ID_MENU_BASE];
        if (enabled[cmd - ID_MENU_BASE] && id && id[0] && g_events && g_events->on_menu_item)
            g_events->on_menu_item(g_events->ctx, id);
    }
    for (i = 0; i < n; i++) free(ids[i]);
    free(ids);
    free(enabled);
}

/* ---------------------------------------------------------------- post queue (any thread -> UI) */

static void drain_posts(void)
{
    for (;;) {
        post_node *n;
        kt_mutex_lock(&g_post_mu);
        n = g_post_head;
        if (n) {
            g_post_head = n->next;
            if (!g_post_head) g_post_tail = NULL;
        }
        kt_mutex_unlock(&g_post_mu);
        if (!n) break;
        n->fn(n->arg);
        free(n);
    }
}

/* ---------------------------------------------------------------- window procedure */

static LRESULT CALLBACK wnd_proc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp)
{
    if (g_wm_taskbar && msg == g_wm_taskbar) {
        /* explorer restarted: the taskbar forgot our icon.  Rebuild (metrics may have changed) and re-add. */
        build_icons();
        g_icon_added = 0;
        icon_add();
        return 0;
    }
    switch (msg) {
    case WM_KT_TRAY: {
        UINT event = LOWORD(lp);
        if (g_v4 && HIWORD(lp) != TRAY_UID) return 0;   /* not our icon */
        switch (event) {
        case WM_CONTEXTMENU:       /* v4: right click or the menu key (WM_RBUTTONUP also arrives: ignored) */
            show_popup_menu(g_v4, GET_X_LPARAM(wp), GET_Y_LPARAM(wp));
            break;
        case WM_RBUTTONUP:         /* legacy callbacks only (no NIM_SETVERSION): there is no WM_CONTEXTMENU */
            if (!g_v4) show_popup_menu(0, 0, 0);
            break;
        case NIN_KEYSELECT:        /* Enter/Space on the focused icon */
        case WM_LBUTTONDBLCLK:
            if (g_events && g_events->on_default_action) g_events->on_default_action(g_events->ctx);
            break;
        default:
            break;
        }
        return 0;
    }
    case WM_KT_POST:
        drain_posts();
        return 0;
    case WM_DPICHANGED:
    case WM_SETTINGCHANGE:
    case WM_DISPLAYCHANGE:
        build_icons();   /* the small-icon size may have changed */
        icon_update();
        return 0;
    case WM_CLOSE:
        DestroyWindow(hwnd);
        return 0;
    case WM_ENDSESSION:
        if (wp) icon_remove();
        return 0;
    case WM_DESTROY:
        g_destroyed = 1;
        wd_close_all();
        icon_remove();
        PostQuitMessage(0);
        return 0;
    default:
        return DefWindowProcW(hwnd, msg, wp, lp);
    }
}

/* ---------------------------------------------------------------- kt_view implementation */

static void v_bind(void *ctx, const kt_events *e)
{
    (void)ctx;
    g_events = e;
    wd_set_events(e);
}

static int v_claim_single_instance(void *ctx)
{
    (void)ctx;
    g_single_mutex = CreateMutexW(NULL, FALSE, L"Local\\kiko-tray-single-instance");
    if (g_single_mutex && GetLastError() == ERROR_ALREADY_EXISTS) {
        CloseHandle(g_single_mutex);
        g_single_mutex = NULL;
        return 0;
    }
    return 1;
}

static void v_set_state(void *ctx, kt_state st, const char *tooltip)
{
    wchar_t *w = kt_win_widen(tooltip);
    (void)ctx;
    g_state = st;
    wcsncpy(g_tip, w ? w : L"KIKO", (sizeof g_tip / sizeof g_tip[0]) - 1);
    g_tip[(sizeof g_tip / sizeof g_tip[0]) - 1] = L'\0';
    free(w);
    icon_update();
}

static void v_set_menu(void *ctx, const kt_menu_item *items, int n)
{
    int i;
    (void)ctx;
    menu_free();
    g_menu = (menu_entry *)calloc((size_t)n, sizeof *g_menu);
    if (!g_menu) return;
    g_menu_n = n;
    for (i = 0; i < n; i++) {
        g_menu[i].label = kt_win_widen(items[i].label ? items[i].label : "");
        g_menu[i].id = kt_strdup(items[i].id ? items[i].id : "");
        g_menu[i].enabled = items[i].enabled;
        g_menu[i].separator = items[i].separator;
    }
}

static void v_show_dialog(void *ctx, const kt_dialog *d) { (void)ctx; wd_show(d); }

static void v_notify(void *ctx, const char *text)
{
    NOTIFYICONDATAW n;
    wchar_t *w = kt_win_widen(text);
    (void)ctx;
    if (!w || !g_icon_added) { free(w); return; }
    fill_nid(&n, NIF_INFO);
    wcsncpy(n.szInfo, w, (sizeof n.szInfo / sizeof n.szInfo[0]) - 1);
    wcsncpy(n.szInfoTitle, L"kiko", (sizeof n.szInfoTitle / sizeof n.szInfoTitle[0]) - 1);
    n.dwInfoFlags = NIIF_INFO;
    Shell_NotifyIconW(NIM_MODIFY, &n);
    free(w);
}

static void v_open_key_store(void *ctx)
{
    (void)ctx;
    /* control.exe /name Microsoft.CredentialManager  (fixed strings; nothing from the network) */
    ShellExecuteW(NULL, L"open", L"control.exe", L"/name Microsoft.CredentialManager", NULL, SW_SHOWNORMAL);
}

static void v_post_to_ui(void *ctx, kt_ui_fn fn, void *arg)
{
    post_node *n = (post_node *)malloc(sizeof *n);
    (void)ctx;
    if (!n) return;
    n->fn = fn;
    n->arg = arg;
    n->next = NULL;
    kt_mutex_lock(&g_post_mu);
    if (g_post_tail) g_post_tail->next = n; else g_post_head = n;
    g_post_tail = n;
    kt_mutex_unlock(&g_post_mu);
    if (g_hwnd) PostMessageW(g_hwnd, WM_KT_POST, 0, 0);
}

static void v_run(void *ctx)
{
    MSG msg;
    (void)ctx;
    while (GetMessageW(&msg, NULL, 0, 0) > 0) {
        if (wd_is_dialog_message(&msg)) continue;
        TranslateMessage(&msg);
        DispatchMessageW(&msg);
    }
}

static void v_quit(void *ctx)
{
    (void)ctx;
    if (g_hwnd) PostMessageW(g_hwnd, WM_CLOSE, 0, 0);   /* safe from any thread */
}

int win_view_create(HINSTANCE hinst, kt_view *out)
{
    WNDCLASSEXW wc;

    g_hinst = hinst;
    kt_mutex_init(&g_post_mu);
    /* ShellExecuteW needs COM on this thread */
    if (SUCCEEDED(CoInitializeEx(NULL, COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE))) g_com_init = 1;
    g_wm_taskbar = RegisterWindowMessageW(L"TaskbarCreated");
    if (wd_init(hinst) != 0) return -1;

    memset(&wc, 0, sizeof wc);
    wc.cbSize = sizeof wc;
    wc.lpfnWndProc = wnd_proc;
    wc.hInstance = hinst;
    wc.lpszClassName = L"KikoTrayMessageWindow";
    if (!RegisterClassExW(&wc)) return -1;
    /* A hidden TOP-LEVEL window, not a message-only one: message-only windows do not receive the
     * TaskbarCreated broadcast. */
    g_hwnd = CreateWindowExW(WS_EX_TOOLWINDOW, wc.lpszClassName, L"kiko-tray", WS_POPUP, 0, 0, 0, 0,
                             NULL, NULL, hinst, NULL);
    if (!g_hwnd) return -1;
    build_icons();
    /* the icon is added on the first set_state, after the single-instance check */

    memset(out, 0, sizeof *out);
    out->ctx = NULL;
    out->key_store_label = "Open Credential Manager";
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

void win_view_destroy(void)
{
    g_events = NULL;   /* the presenter is already freed: no callbacks while closing */
    wd_set_events(NULL);
    if (g_hwnd && !g_destroyed) DestroyWindow(g_hwnd);   /* removes the tray icon via WM_DESTROY */
    g_hwnd = NULL;
    icon_remove();
    free_icons();
    menu_free();
    if (g_single_mutex) { CloseHandle(g_single_mutex); g_single_mutex = NULL; }
    if (g_hinst) UnregisterClassW(L"KikoTrayMessageWindow", g_hinst);
    /* Posted functions that never ran belong to a presenter that is gone: drop the nodes, never call them
     * (a late callback could re-add the tray icon). */
    kt_mutex_lock(&g_post_mu);
    while (g_post_head) {
        post_node *n = g_post_head;
        g_post_head = n->next;
        free(n);
    }
    g_post_tail = NULL;
    kt_mutex_unlock(&g_post_mu);
    if (g_com_init) { CoUninitialize(); g_com_init = 0; }
}
