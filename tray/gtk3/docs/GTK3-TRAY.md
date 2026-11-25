# GTK3 Tray Implementation — Technical Documentation

> Part of kiko-tray: native system tray for kiko (model router).
> Platform: Linux Mint (Cinnamon), XFCE, GNOME, other GTK3 desktops.

---

## Overview

`gtk_view.c` implements the `kt_view` interface for GTK3 desktops. It provides:

- **Tray icon**: `XAppStatusIcon` (preferred, Linux Mint/Cinnamon) with fallback to `GtkStatusIcon` (deprecated but universal)
- **Menu**: Standard `GtkMenu` with items mapped to `kt_menu_item` IDs
- **Dialogs**: `GtkTreeView` (tables) + `GtkTextView` (logs/text) in `GtkWindow`
- **Notifications**: `notify-send` (freedesktop standard)
- **Key store opener**: `seahorse` (GNOME Passwords and Keys)
- **Single-instance lock**: `flock()` on `$XDG_RUNTIME_DIR/kiko-tray.lock`
- **Thread-safe UI posts**: `g_idle_add()` from worker threads

All GTK code runs on the **main thread** (GTK requirement). Only `post_to_ui` and `quit` are callable from any thread.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                      kt_presenter (common)                      │
│  State machine, polling, API calls, dialog builders             │
└────────────────────────────────┬────────────────────────────────┘
                                 │ kt_view interface (C function pointers)
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│                      gtk_view.c (this file)                     │
├─────────────────────────────────────────────────────────────────┤
│  v_bind()              → stores kt_events*                      │
│  v_claim_single_instance() → flock() lock file                  │
│  v_set_state()         → XAppStatusIcon / GtkStatusIcon + tooltip│
│  v_set_menu()          → GtkMenu rebuild                        │
│  v_show_dialog()       → GtkWindow + GtkTreeView / GtkTextView  │
│  v_notify()            → notify-send subprocess                 │
│  v_open_key_store()    → seahorse subprocess                    │
│  v_post_to_ui()        → g_idle_add() (thread-safe)             │
│  v_run()               → gtk_main()                             │
│  v_quit()              → g_idle_add(gtk_main_quit)              │
└─────────────────────────────────────────────────────────────────┘
```

---

## Build Configuration

### CMake (from `tray/CMakeLists.txt`)

```cmake
find_package(PkgConfig REQUIRED)
pkg_check_modules(GTK3 REQUIRED gtk+-3.0)
pkg_check_modules(XAPP xapp)   # optional

add_executable(kiko-tray gtk3/gtk_main.c gtk3/gtk_view.c gtk3/gtk_host.c)
target_include_directories(kiko-tray PRIVATE gtk3 ${GTK3_INCLUDE_DIRS})
target_link_libraries(kiko-tray PRIVATE kt_common ${GTK3_LIBRARIES})
target_compile_options(kiko-tray PRIVATE ${GTK3_CFLAGS_OTHER} -Wno-deprecated-declarations)

if(XAPP_FOUND)
  target_compile_definitions(kiko-tray PRIVATE KT_HAVE_XAPP=1)
  target_include_directories(kiko-tray PRIVATE ${XAPP_INCLUDE_DIRS})
  target_link_libraries(kiko-tray PRIVATE ${XAPP_LIBRARIES})
endif()
```

### Compile Definitions

| Define | Meaning |
|--------|---------|
| `KT_HAVE_XAPP` | `libxapp` found → use `XAppStatusIcon` |
| (none) | Fallback to `GtkStatusIcon` |

### Dependencies

| Runtime | Build |
|---------|-------|
| `libgtk-3-0` | `libgtk-3-dev` |
| `libxapp1` (Mint) | `libxapp-dev` |
| `notify-send` | `libnotify-bin` |
| `seahorse` | `seahorse` |

---

## Tray Icon Implementation

### Two Backends

```c
#ifdef KT_HAVE_XAPP
static XAppStatusIcon *g_xicon;
#else
static GtkStatusIcon *g_sicon;
#endif
```

#### XAppStatusIcon (Preferred — Linux Mint/Cinnamon)

```c
g_xicon = xapp_status_icon_new();
xapp_status_icon_set_name(g_xicon, "kiko-tray");
xapp_status_icon_set_icon_name(g_xicon, g_png[KT_STATE_STARTING]);  // PNG file path
xapp_status_icon_set_tooltip_text(g_xicon, "KIKO");
xapp_status_icon_set_visible(g_xicon, TRUE);
g_signal_connect(g_xicon, "activate", G_CALLBACK(on_xapp_activate), NULL);
// Right-click menu:
xapp_status_icon_set_secondary_menu(g_xicon, GTK_MENU(g_menu));
```

**Advantages:**
- Native Cinnamon panel integration
- HiDPI support (loads PNG at correct scale)
- Persistent across panel restarts
- Standard freedesktop `StatusNotifierItem` compatible

#### GtkStatusIcon (Fallback — Deprecated)

```c
g_sicon = gtk_status_icon_new_from_pixbuf(g_pix[KT_STATE_STARTING]);
gtk_status_icon_set_title(g_sicon, "kiko-tray");
gtk_status_icon_set_visible(g_sicon, TRUE);
g_signal_connect(g_sicon, "popup-menu", G_CALLBACK(on_sicon_popup), NULL);
g_signal_connect(g_sicon, "activate", G_CALLBACK(on_sicon_activate), NULL);
```

**Limitations:**
- Deprecated since GTK 3.14 (2014)
- No HiDPI support on some desktops
- Removed in GTK4
- Menu positioning quirks on GNOME Shell

### Icon Rendering

Icons are **generated in-memory** by `kt_icon.c` (common core):

```c
// kt_icon_render(size, state, bgra, pixels)
// state: KT_STATE_STARTING(0) | KT_STATE_OK(1) | KT_STATE_DEGRADED(2) | KT_STATE_DOWN(3)
// Colors: Blue / Green / Orange / Red
```

For XApp: rendered at 48px → saved as PNG in `$XDG_CACHE_HOME/kiko-tray/state-{0,1,2,3}.png`
For GtkStatusIcon: rendered at 22px → kept as `GdkPixbuf`

---

## Menu System

### Menu Rebuild (called from presenter on state change)

```c
static void v_set_menu(void *ctx, const kt_menu_item *items, int n)
{
    GtkWidget *menu = gtk_menu_new();
    // ... create GtkMenuItem / GtkSeparatorMenuItem for each item
    // Store item ID in GObject data: g_object_set_data_full(item, "kt-id", g_strdup(id), g_free)
    // Connect "activate" → on_menu_activate()
    
    // Atomic swap:
    GtkWidget *old = g_menu;
    g_menu = g_object_ref_sink(menu);
    if (g_xicon) xapp_status_icon_set_secondary_menu(g_xicon, GTK_MENU(g_menu));
    if (old) g_object_unref(old);
}
```

### Menu Item Activation

```c
static void on_menu_activate(GtkMenuItem *item, gpointer data)
{
    const char *id = g_object_get_data(G_OBJECT(item), "kt-id");
    if (id && g_events && g_events->on_menu_item)
        g_events->on_menu_item(g_events->ctx, id);
}
```

**IDs passed to presenter** (defined in `kt_presenter.h`):
- `KT_ID_START`, `KT_ID_STOP`, `KT_ID_RESTART`
- `KT_ID_STATUS`, `KT_ID_LOGS`, `KT_ID_MODELS`, `KT_ID_CLASSES`, `KT_ID_KEYS`, `KT_ID_COSTS`, `KT_ID_SETTINGS`, `KT_ID_ABOUT`
- `KT_ID_QUIT`

---

## Dialogs

### Dialog Management

- **One window per `kind`** (status, logs, models, classes, keys, costs, settings, about)
- **Update in place**: if window exists, destroy child content and rebuild
- **Tracked in linked list** (`g_dialogs`) for cleanup on shutdown

### Table Dialogs (models, classes, keys, costs)

```c
// kt_dialog has: ncols, nrows, columns[], cells[] (flat array), summary, footer, buttons[]
// Built by presenter: build_table() → kt_dialog*
// Rendered here: make_table() → GtkTreeView + GtkListStore
```

### Text Dialogs (logs, status, settings, about)

```c
// kt_dialog has: text (full string)
// Rendered here: make_text() → GtkTextView (monospace, read-only, auto-scroll to end)
```

### Buttons

Each dialog can have buttons (e.g., "Refresh", "Open keyring"):
```c
// kt_dialog.buttons[]: { label, id }
// Connected to on_dialog_button() → presenter.on_dialog_button(kind, button_id)
```

---

## Thread Safety

### The Rule

**Only `v_post_to_ui()` and `v_quit()` may be called from non-GTK threads.**

Everything else (state, menu, dialogs, notifications) runs on the GTK main thread via `g_idle_add()`.

### Implementation

```c
static gboolean post_cb(gpointer data)
{
    post_item *it = (post_item *)data;
    it->fn(it->arg);      // Execute the posted function
    g_free(it);
    return G_SOURCE_REMOVE;
}

static void v_post_to_ui(void *ctx, kt_ui_fn fn, void *arg)
{
    post_item *it = g_new(post_item, 1);
    it->fn = fn;
    it->arg = arg;
    g_idle_add(post_cb, it);   // Thread-safe: queues callback on main loop
}
```

### Used By Presenter For

- `ui_apply_state` → `v_set_state` + `rebuild_menu`
- `ui_notify` → `v_notify`
- `ui_show_dialog` → `v_show_dialog`
- `ui_action_done` → `rebuild_menu` + `poll_now`
- `dialog_run` → builds dialog in worker → posts `ui_show_dialog`

---

## Single Instance Lock

```c
static int v_claim_single_instance(void *ctx)
{
    const char *rt = g_get_user_runtime_dir();  // $XDG_RUNTIME_DIR
    gchar *path = g_build_filename(rt ? rt : g_get_tmp_dir(), "kiko-tray.lock", NULL);
    g_lock_fd = open(path, O_CREAT | O_RDWR | O_CLOEXEC, 0600);
    if (g_lock_fd < 0) return 1;   // Cannot lock: don't block user
    if (flock(g_lock_fd, LOCK_EX | LOCK_NB) != 0) { close(g_lock_fd); g_lock_fd = -1; return 0; }
    return 1;
}
```

- Lock file: `$XDG_RUNTIME_DIR/kiko-tray.lock` (or `/tmp/kiko-tray.lock`)
- `LOCK_EX | LOCK_NB` → non-blocking exclusive lock
- Returns 0 if another instance holds the lock
- Released automatically on process exit (FD closed)

---

## Notifications

```c
static void v_notify(void *ctx, const char *text)
{
    char *argv[7] = { "notify-send", "-a", "kiko-tray", "--", "kiko", text, NULL };
    spawn_quiet(argv);   // g_spawn_async with stdout/stderr to /dev/null
}
```

- Uses `notify-send` (freedesktop standard)
- App name: "kiko-tray"
- Summary: "kiko"
- Body: the notification text

---

## Key Store Integration

```c
static void v_open_key_store(void *ctx)
{
    char *argv[2] = { "seahorse", NULL };
    if (!g_spawn_async(NULL, argv, NULL, G_SPAWN_SEARCH_PATH, NULL, NULL, NULL, &err)) {
        v_notify(NULL, "Could not start seahorse (sudo apt install seahorse)");
        g_clear_error(&err);
    }
}
```

- Opens **Seahorse** (GNOME Passwords and Keys)
- User can view/edit/delete `kiko-identity` entries
- Fallback notification if not installed

---

## Lifecycle

### Create

```c
int gtk_view_create(kt_view *out)
{
    build_icons();           // Render 4 state icons (48px PNG + 22px Pixbuf)
    create_tray_icon();      // XAppStatusIcon or GtkStatusIcon
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
```

### Run

```c
static void v_run(void *ctx) { (void)ctx; gtk_main(); }
static void v_quit(void *ctx) { (void)ctx; g_idle_add(quit_cb, NULL); }
```

### Destroy

```c
void gtk_view_destroy(void)
{
    g_events = NULL;
    while (g_dialogs) gtk_widget_destroy(g_dialogs->win);
    if (g_xicon) { xapp_status_icon_set_visible(g_xicon, FALSE); g_object_unref(g_xicon); g_xicon = NULL; }
    if (g_sicon) { gtk_status_icon_set_visible(g_sicon, FALSE); g_object_unref(g_sicon); g_sicon = NULL; }
    if (g_menu) { g_object_unref(g_menu); g_menu = NULL; }
    for (i = 0; i < 4; i++) { if (g_pix[i]) g_object_unref(g_pix[i]); g_pix[i] = NULL; g_free(g_png[i]); g_png[i] = NULL; }
    if (g_lock_fd >= 0) { close(g_lock_fd); g_lock_fd = -1; }
}
```

---

## Known Issues / TODO

| Issue | Priority | Notes |
|-------|----------|-------|
| `GtkStatusIcon` deprecated | High | GTK4 removes it. Need AppIndicator (`libayatana-appindicator`) or `StatusNotifierItem` DBUS implementation for non-Cinnamon desktops |
| No HiDPI on GtkStatusIcon fallback | Medium | XApp handles it via PNG files |
| Menu positioning on GNOME Shell | Low | GtkStatusIcon menu appears at wrong position sometimes |
| `notify-send` may not exist | Low | Most desktops have it; could implement DBUS `org.freedesktop.Notifications` directly |
| Single-instance lock on NFS home | Low | `$XDG_RUNTIME_DIR` is local, so OK |
| Dialog memory: `g_object_ref_sink` on menu | Medium | Current code is correct but subtle |

---

## Testing

### Manual

```bash
# Build
cmake -B build -S ../.. -DKT_GUI=gtk -DKT_BUILD_GUI=ON
cmake --build build

# Run (needs running kiko-lite on port 4000)
./build/kiko-tray
```

### Unit Tests (CTest)

Core logic tests in `common/tests/`:
- `test_presenter` — state machine, polling, dialog builders
- `test_json`, `test_http`, `test_mask`, etc.

No GUI tests (headless CI).

---

## Files

| File | Purpose |
|------|---------|
| `gtk_main.c` | `main()`: `gtk_init()`, `gtk_view_create()`, `kt_presenter_new()`, `kt_presenter_start()`, `gtk_view_destroy()` |
| `gtk_view.c` | **This file** — `kt_view` implementation |
| `gtk_host.c` | `kt_host` implementation: `systemctl --user start/stop/restart kiko-lite` |
| `gtk_internal.h` | Private declarations |
| `README.md` | User-facing build/install docs |
| `docs/GTK3-TRAY.md` | **This file** — technical documentation |

---

## License

MIT — Copyright Peter Kokosharov <peter.kokosharov@gmail.com>