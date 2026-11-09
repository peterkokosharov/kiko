# GUI layering of kiko-tray (native C/C++)

Like Mozilla: one portable core, one thin GUI layer per OS. Status: **written, never compiled** (the
authoring machine has no C compiler; see BUILD.md and the test plans for how to verify).

```
native/
  common/    portable C11, no GUI: state model, menu model, presenter, HTTP client, JSON (cJSON wrapper),
             key masking, worker thread, icon renderer, dialog model, the kt_view interface
  win32/     Windows: Win32 API only (hidden window, Shell_NotifyIconW, native dialogs)
  gtk/       Linux Mint / Cinnamon: GTK3 + XAppStatusIcon (GtkStatusIcon fallback)
  qt/        KDE Plasma (also usable elsewhere): C++17, Qt6 or Qt5 Widgets, QSystemTrayIcon
  macos/     macOS: Objective-C (ARC), NSStatusItem, NSMenu, NSWindow
  tests/     CTest, pure C tests of common/ (fake view, fake HTTP, loopback socket)
  third_party/cjson/   cJSON 1.7.18 (vendored or fetched by CMake)
```

## What is common and what is per-OS

| Common (`native/common`) | Per OS (`native/<layer>`) |
|---|---|
| polling kiko `/health`, state machine (starting/ok/degraded/down) | the tray icon (draw/show/update) |
| menu content and enabled flags | rendering the menu, popup, click handling |
| dialog **content** (`kt_dialog`: summary, table or text, buttons) | native dialog windows |
| all HTTP+JSON to `127.0.0.1` (API-CONTRACT.md) | start/stop of kiko (`kt_host.service`) |
| key masking (`NAME: *****`), log redaction | opening the OS key store |
| state icon pixels (`kt_icon_render`) | turning pixels into an OS icon |
| worker threads | the event loop and `post_to_ui` |

## The interface (`common/kt_view.h`)

A GUI layer fills a `kt_view` (struct of function pointers) and gets a `kt_events` from `bind`:

```
kt_view:   bind, claim_single_instance, set_state(state, tooltip), set_menu(items, n),
           show_dialog(kt_dialog*), notify(text), open_key_store(), post_to_ui(fn, arg),
           run(), quit()            + key_store_label (text of the wallet button, or NULL)
kt_events: on_menu_item(id), on_default_action() [icon double-click -> Status],
           on_dialog_button(kind, button_id), on_dialog_closed(kind)
kt_host:   service(action start|stop|restart, msg)      (blocking, runs on a worker)
```

Strings are UTF-8. Pointers passed to a view function are valid only during the call; the view copies
what it keeps.

## THREADING RULE (the one rule that matters)

1. **Every `kt_view` function and every `kt_events` callback runs on the UI thread**, except `post_to_ui`
   and `quit`, which may be called from any thread.
2. **The UI thread never blocks**: no HTTP, no process control, no file reads. The presenter does that on
   its worker thread (one job queue) and a poll thread, and hands every result back through
   `post_to_ui(fn, arg)`.
3. Presenter state is touched only on the UI thread; workers use immutable configuration.
4. `kt_presenter_free` is called after `run()` returned; views must stop calling events during their own
   destroy (they set their events pointer to NULL first).

`post_to_ui` per layer: Win32 `PostMessage` to the hidden window + queue; GTK `g_idle_add`; Qt
`QMetaObject::invokeMethod(qApp, ..., Qt::QueuedConnection)`; macOS `dispatch_async(main queue)`.

## Adding a new GUI layer (checklist)

1. Create `native/<name>/` with a `*_main` (reads config: `KIKO_PORT`, `KIKO_API_TOKEN` or
   `~/.kiko/api-token`; fills `kt_config`), a `*_view` (implements `kt_view`) and a `*_host`
   (implements `kt_host.service` for that OS).
2. Implement all `kt_view` functions; call `kt_events` as described; copy anything you keep.
3. Draw state icons with `kt_icon_render(size, state, bgra?, buffer)` (straight alpha); no icon files.
4. Dialogs: render `kt_dialog` (summary label; table if `ncols > 0` else read-only text; footer; buttons +
   your own Close). One window per `kind`; showing an open kind updates and re-focuses it.
5. Single instance (`claim_single_instance`), quit that removes the icon, `post_to_ui` that is thread safe.
6. Add a branch to `native/CMakeLists.txt`; document the build and a test plan in `docs/`.

## Adding the TQt layer (Q4OS / Trinity, C++)

The core is C with `extern "C"` guards in `kt_view.h`/`kt_presenter.h`; wrap the other includes in
`extern "C" { }` (see `native/qt/qt_internal.h`). TQt (Qt3-based) differs from Qt5/6 in API names but not
in the shape of the work: `TQSystemTray` (XEmbed tray) for the icon, `TQPopupMenu` for the menu,
`TQDialog` + `TQTable` + `TQTextEdit` for dialogs, a `TQCustomEvent` posted with `tqApp->postEvent` for
`post_to_ui`. Copy `native/qt/` as the starting point, keep `kt_host.service` as `systemctl --user` (or
the init system of the target), and add a `KT_GUI=tqt` branch to CMake.

## macOS and Qt layers

`native/macos` and `native/qt` are skeletons "as complete as possible without a compiler". They follow
the same interface; their test plans are `TEST-PLAN-MACOS.md` and `TEST-PLAN-PLASMA.md`. macOS notes:
coloured status icon (not a template image), `LSUIElement` agent app (no Dock icon), notifications through
AppleScript `display notification`, start/stop through `launchctl` for `docs/macos/com.kiko.lite.plist`.
Qt notes: no `Q_OBJECT` classes (no moc step), KDE Wallet manager looked up as
`kwalletmanager6` / `kwalletmanager5` / `kwalletmanager`.
