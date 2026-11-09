/* SPDX-License-Identifier: MIT
 * Author: Peter Kokosharov <peter.kokosharov@gmail.com>
 *
 * win_dialogs.c - native dialog windows built from a kt_dialog: STATIC labels, SysListView32 table or a
 * read-only multi-line EDIT, push buttons.  Segoe UI (the system message font), per-monitor DPI aware.
 * One window per dialog kind: showing a kind that is open updates its controls and re-focuses it.
 */
#include "win_internal.h"

#include <commctrl.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

#include "kt_util.h"

#define DLG_CLASS L"KikoTrayDialog"
#define ID_BTN_BASE 2000

typedef struct dlg {
    HWND        hwnd;
    char       *kind;
    HFONT       font;
    int         font_owned;
    UINT        dpi;
    HWND        summary, body, footer, close;
    HWND       *buttons;
    char      **btn_ids;
    int         nbuttons;
    int         has_footer;
    struct dlg *next;
} dlg;

static HINSTANCE        g_hinst;
static const kt_events *g_events;
static dlg             *g_list;

static int px(const dlg *d, int v) { return MulDiv(v, (int)d->dpi, 96); }

/* ---------------------------------------------------------------- DPI and font */

UINT wd_dpi_for_window(HWND hwnd)
{
    typedef UINT (WINAPI *get_dpi_fn)(HWND);
    static get_dpi_fn fn;
    static int looked;
    UINT dpi = 0;
    if (!looked) {
        HMODULE u = GetModuleHandleW(L"user32.dll");
        fn = u ? (get_dpi_fn)(void *)GetProcAddress(u, "GetDpiForWindow") : NULL;
        looked = 1;
    }
    if (fn && hwnd) dpi = fn(hwnd);
    if (dpi == 0) {
        HDC dc = GetDC(hwnd);
        if (dc) { dpi = (UINT)GetDeviceCaps(dc, LOGPIXELSX); ReleaseDC(hwnd, dc); }
    }
    return dpi ? dpi : 96;
}

static void make_font(dlg *d)
{
    typedef BOOL (WINAPI *spi_dpi_fn)(UINT, UINT, PVOID, UINT, UINT);
    NONCLIENTMETRICSW ncm;
    HMODULE u = GetModuleHandleW(L"user32.dll");
    spi_dpi_fn spi = u ? (spi_dpi_fn)(void *)GetProcAddress(u, "SystemParametersInfoForDpi") : NULL;
    BOOL ok;

    if (d->font && d->font_owned) DeleteObject(d->font);
    d->font = NULL;
    d->font_owned = 0;
    memset(&ncm, 0, sizeof ncm);
    ncm.cbSize = sizeof ncm;
    if (spi) ok = spi(SPI_GETNONCLIENTMETRICS, sizeof ncm, &ncm, 0, d->dpi);
    else ok = SystemParametersInfoW(SPI_GETNONCLIENTMETRICS, sizeof ncm, &ncm, 0);
    if (ok) {
        d->font = CreateFontIndirectW(&ncm.lfMessageFont);   /* Segoe UI 9pt on Windows 10/11 */
        d->font_owned = d->font != NULL;
    }
    if (!d->font) d->font = (HFONT)GetStockObject(DEFAULT_GUI_FONT);
}

static void apply_font(dlg *d)
{
    int i;
    HWND all[4];
    all[0] = d->summary; all[1] = d->body; all[2] = d->footer; all[3] = d->close;
    for (i = 0; i < 4; i++)
        if (all[i]) SendMessageW(all[i], WM_SETFONT, (WPARAM)d->font, TRUE);
    for (i = 0; i < d->nbuttons; i++)
        if (d->buttons[i]) SendMessageW(d->buttons[i], WM_SETFONT, (WPARAM)d->font, TRUE);
}

/* ---------------------------------------------------------------- layout */

static void layout(dlg *d)
{
    RECT rc;
    int m, w, h, y, sum_h, foot_h, btn_h, btn_w, gap, i, x, body_h;

    if (!d->summary || !d->body || !d->close || !GetClientRect(d->hwnd, &rc)) return;
    m = px(d, 12);
    gap = px(d, 8);
    w = rc.right - 2 * m;
    h = rc.bottom;
    sum_h = px(d, 36);
    foot_h = d->has_footer ? px(d, 36) : 0;
    btn_h = px(d, 30);
    btn_w = px(d, 110);
    if (w < 50) w = 50;

    y = m;
    MoveWindow(d->summary, m, y, w, sum_h, TRUE);
    y += sum_h + gap;
    body_h = h - y - m - btn_h - gap - (foot_h ? foot_h + gap : 0);
    if (body_h < px(d, 40)) body_h = px(d, 40);
    MoveWindow(d->body, m, y, w, body_h, TRUE);
    y += body_h + gap;
    if (foot_h && d->footer) {
        MoveWindow(d->footer, m, y, w, foot_h, TRUE);
        y += foot_h + gap;
    }
    /* custom buttons on the left, Close on the right */
    x = m;
    for (i = 0; i < d->nbuttons; i++) {
        MoveWindow(d->buttons[i], x, y, btn_w + px(d, 40), btn_h, TRUE);
        x += btn_w + px(d, 40) + gap;
    }
    MoveWindow(d->close, m + w - btn_w, y, btn_w, btn_h, TRUE);
}

/* ---------------------------------------------------------------- controls from the model */

static HWND make_child(dlg *d, const wchar_t *cls, const wchar_t *text, DWORD style, DWORD ex, int id)
{
    return CreateWindowExW(ex, cls, text ? text : L"", WS_CHILD | WS_VISIBLE | style, 0, 0, 10, 10, d->hwnd,
                           (HMENU)(UINT_PTR)id, g_hinst, NULL);
}

static void destroy_controls(dlg *d)
{
    int i;
    if (d->summary) DestroyWindow(d->summary);
    if (d->body) DestroyWindow(d->body);
    if (d->footer) DestroyWindow(d->footer);
    if (d->close) DestroyWindow(d->close);
    for (i = 0; i < d->nbuttons; i++) {
        if (d->buttons[i]) DestroyWindow(d->buttons[i]);
        free(d->btn_ids[i]);
    }
    free(d->buttons);
    free(d->btn_ids);
    d->summary = d->body = d->footer = d->close = NULL;
    d->buttons = NULL;
    d->btn_ids = NULL;
    d->nbuttons = 0;
}

static void fill_table(dlg *d, const kt_dialog *m)
{
    LVCOLUMNW col;
    LVITEMW it;
    int c, r;

    SendMessageW(d->body, LVM_SETEXTENDEDLISTVIEWSTYLE, LVS_EX_FULLROWSELECT, LVS_EX_FULLROWSELECT);
    for (c = 0; c < m->ncols; c++) {
        wchar_t *name = kt_win_widen(m->columns[c]);
        memset(&col, 0, sizeof col);
        col.mask = LVCF_TEXT | LVCF_WIDTH | LVCF_FMT | LVCF_SUBITEM;
        col.fmt = LVCFMT_LEFT;
        col.cx = px(d, 140);
        col.pszText = name ? name : L"";
        col.iSubItem = c;
        SendMessageW(d->body, LVM_INSERTCOLUMNW, (WPARAM)c, (LPARAM)&col);
        free(name);
    }
    for (r = 0; r < m->nrows; r++) {
        for (c = 0; c < m->ncols; c++) {
            wchar_t *cell = kt_win_widen(m->cells[r * m->ncols + c]);
            memset(&it, 0, sizeof it);
            it.mask = LVIF_TEXT;
            it.iItem = r;
            it.iSubItem = c;
            it.pszText = cell ? cell : L"";
            if (c == 0) SendMessageW(d->body, LVM_INSERTITEMW, 0, (LPARAM)&it);
            else SendMessageW(d->body, LVM_SETITEMTEXTW, (WPARAM)r, (LPARAM)&it);
            free(cell);
        }
    }
    for (c = 0; c < m->ncols; c++)
        SendMessageW(d->body, LVM_SETCOLUMNWIDTH, (WPARAM)c, (LPARAM)LVSCW_AUTOSIZE_USEHEADER);
}

static void fill_text(dlg *d, const kt_dialog *m)
{
    int len;
    wchar_t *w = kt_win_widen_crlf(m->text);
    SendMessageW(d->body, EM_SETLIMITTEXT, 0, 0);
    SetWindowTextW(d->body, w ? w : L"");
    len = GetWindowTextLengthW(d->body);
    SendMessageW(d->body, EM_SETSEL, (WPARAM)len, (LPARAM)len);   /* show the newest line (logs) */
    SendMessageW(d->body, EM_SCROLLCARET, 0, 0);
    free(w);
}

static void build_controls(dlg *d, const kt_dialog *m)
{
    wchar_t *w;
    int i;

    destroy_controls(d);
    w = kt_win_widen(m->summary);
    d->summary = make_child(d, L"STATIC", w, SS_LEFT | SS_NOPREFIX, 0, 100);
    free(w);

    if (m->ncols > 0) {
        d->body = make_child(d, WC_LISTVIEWW, NULL,
                             WS_TABSTOP | WS_BORDER | LVS_REPORT | LVS_SHOWSELALWAYS | LVS_SINGLESEL,
                             WS_EX_CLIENTEDGE, 101);
        if (d->body) fill_table(d, m);
    } else {
        d->body = make_child(d, L"EDIT", NULL,
                             WS_TABSTOP | WS_VSCROLL | WS_HSCROLL | ES_MULTILINE | ES_READONLY | ES_AUTOVSCROLL
                                 | ES_AUTOHSCROLL,
                             WS_EX_CLIENTEDGE, 101);
        if (d->body) fill_text(d, m);
    }

    d->has_footer = m->footer && m->footer[0];
    if (d->has_footer) {
        w = kt_win_widen(m->footer);
        d->footer = make_child(d, L"STATIC", w, SS_LEFT | SS_NOPREFIX, 0, 102);
        free(w);
    }

    d->buttons = (HWND *)calloc((size_t)(m->nbuttons ? m->nbuttons : 1), sizeof *d->buttons);
    d->btn_ids = (char **)calloc((size_t)(m->nbuttons ? m->nbuttons : 1), sizeof *d->btn_ids);
    if (d->buttons && d->btn_ids) {
        d->nbuttons = m->nbuttons;
        for (i = 0; i < m->nbuttons; i++) {
            w = kt_win_widen(m->buttons[i].label);
            d->buttons[i] = make_child(d, L"BUTTON", w, WS_TABSTOP | BS_PUSHBUTTON, 0, ID_BTN_BASE + i);
            d->btn_ids[i] = kt_strdup(m->buttons[i].id);
            free(w);
        }
    }
    d->close = make_child(d, L"BUTTON", L"Close", WS_TABSTOP | BS_DEFPUSHBUTTON, 0, IDOK);
    apply_font(d);
    layout(d);
}

/* ---------------------------------------------------------------- window procedure */

static dlg *dlg_of(HWND hwnd) { return (dlg *)GetWindowLongPtrW(hwnd, GWLP_USERDATA); }

static void dlg_unlink(dlg *d)
{
    dlg **pp;
    for (pp = &g_list; *pp; pp = &(*pp)->next) {
        if (*pp == d) { *pp = d->next; return; }
    }
}

static LRESULT CALLBACK dlg_proc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp)
{
    dlg *d;
    if (msg == WM_NCCREATE) {
        const CREATESTRUCTW *cs = (const CREATESTRUCTW *)lp;
        SetWindowLongPtrW(hwnd, GWLP_USERDATA, (LONG_PTR)cs->lpCreateParams);
        return DefWindowProcW(hwnd, msg, wp, lp);
    }
    d = dlg_of(hwnd);
    if (!d) return DefWindowProcW(hwnd, msg, wp, lp);

    switch (msg) {
    case WM_SIZE:
        layout(d);
        return 0;
    case WM_GETMINMAXINFO: {
        MINMAXINFO *mm = (MINMAXINFO *)lp;
        mm->ptMinTrackSize.x = px(d, 480);
        mm->ptMinTrackSize.y = px(d, 300);
        return 0;
    }
    case WM_DPICHANGED: {
        const RECT *r = (const RECT *)lp;
        d->dpi = HIWORD(wp) ? HIWORD(wp) : 96;
        make_font(d);
        apply_font(d);
        SetWindowPos(hwnd, NULL, r->left, r->top, r->right - r->left, r->bottom - r->top,
                     SWP_NOZORDER | SWP_NOACTIVATE);
        layout(d);
        return 0;
    }
    case WM_COMMAND: {
        int id = LOWORD(wp);
        if (id == IDOK || id == IDCANCEL) {
            DestroyWindow(hwnd);
        } else if (id >= ID_BTN_BASE && id < ID_BTN_BASE + d->nbuttons && HIWORD(wp) == BN_CLICKED) {
            if (g_events && g_events->on_dialog_button)
                g_events->on_dialog_button(g_events->ctx, d->kind, d->btn_ids[id - ID_BTN_BASE]);
        }
        return 0;
    }
    case WM_CLOSE:
        DestroyWindow(hwnd);
        return 0;
    case WM_DESTROY:
        dlg_unlink(d);
        if (g_events && g_events->on_dialog_closed) g_events->on_dialog_closed(g_events->ctx, d->kind);
        d->hwnd = NULL;
        destroy_controls(d);
        if (d->font && d->font_owned) DeleteObject(d->font);
        SetWindowLongPtrW(hwnd, GWLP_USERDATA, 0);
        free(d->kind);
        free(d);
        return 0;
    default:
        return DefWindowProcW(hwnd, msg, wp, lp);
    }
}

/* ---------------------------------------------------------------- public API */

int wd_init(HINSTANCE hinst)
{
    WNDCLASSEXW wc;
    INITCOMMONCONTROLSEX icc;

    g_hinst = hinst;
    icc.dwSize = sizeof icc;
    icc.dwICC = ICC_LISTVIEW_CLASSES;
    InitCommonControlsEx(&icc);
    memset(&wc, 0, sizeof wc);
    wc.cbSize = sizeof wc;
    wc.lpfnWndProc = dlg_proc;
    wc.hInstance = hinst;
    wc.hCursor = LoadCursorW(NULL, (LPCWSTR)IDC_ARROW);
    wc.hbrBackground = (HBRUSH)(COLOR_BTNFACE + 1);
    wc.lpszClassName = DLG_CLASS;
    if (!RegisterClassExW(&wc) && GetLastError() != ERROR_CLASS_ALREADY_EXISTS) return -1;
    return 0;
}

void wd_set_events(const kt_events *events) { g_events = events; }

static dlg *find_dialog(const char *kind)
{
    dlg *d;
    for (d = g_list; d; d = d->next)
        if (strcmp(d->kind, kind) == 0) return d;
    return NULL;
}

void wd_show(const kt_dialog *m)
{
    dlg *d = find_dialog(m->kind);
    wchar_t *title = kt_win_widen(m->title);

    if (!d) {
        int w, h;
        d = (dlg *)calloc(1, sizeof *d);
        if (!d) { free(title); return; }
        d->kind = kt_strdup(m->kind);
        d->dpi = wd_dpi_for_window(NULL);
        w = MulDiv(680, (int)d->dpi, 96);
        h = MulDiv(460, (int)d->dpi, 96);
        d->hwnd = CreateWindowExW(0, DLG_CLASS, title ? title : L"kiko", WS_OVERLAPPEDWINDOW,
                                  CW_USEDEFAULT, CW_USEDEFAULT, w, h, NULL, NULL, g_hinst, d);
        if (!d->hwnd) { free(d->kind); free(d); free(title); return; }
        d->next = g_list;
        g_list = d;
        d->dpi = wd_dpi_for_window(d->hwnd);   /* the monitor it actually opened on */
        make_font(d);
    } else if (title) {
        SetWindowTextW(d->hwnd, title);
    }
    free(title);

    build_controls(d, m);   /* first build, or an update of an open dialog */
    if (IsIconic(d->hwnd)) ShowWindow(d->hwnd, SW_RESTORE);
    else ShowWindow(d->hwnd, SW_SHOW);
    SetForegroundWindow(d->hwnd);
}

void wd_close_all(void)
{
    while (g_list) {
        dlg *d = g_list;
        HWND h = d->hwnd;
        if (h) {
            DestroyWindow(h);        /* WM_DESTROY unlinks and frees d */
            if (g_list != d) continue;
        }
        /* no window, or DestroyWindow failed: unlink and free the record by hand (never loop forever) */
        g_list = d->next;
        destroy_controls(d);
        if (d->font && d->font_owned) DeleteObject(d->font);
        free(d->kind);
        free(d);
    }
}

int wd_is_dialog_message(MSG *msg)
{
    dlg *d;
    for (d = g_list; d; d = d->next)
        if (d->hwnd && (msg->hwnd == d->hwnd || IsChild(d->hwnd, msg->hwnd)))
            return IsDialogMessageW(d->hwnd, msg) ? 1 : 0;
    return 0;
}
