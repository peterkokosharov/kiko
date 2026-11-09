# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Sonnet
"""The tkinter window (ttk.Notebook, one tab per kiko web-UI panel) and the optional Tk 9 tray icon.

Network calls run in ONE worker thread; results reach the Tk loop through a queue drained with ``after``.
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
import webbrowser
from tkinter import ttk
from typing import Any

from . import client, model

INTERVAL_MS = 5000
TREES = {  # tab -> (columns, row builder, widths)
    "Models": (("alias", "target", "class", "enabled", "key name", "key found", "health", "last check", "state"),
               model.model_rows),
    "Requests": (("ok", "time", "model", "route", "latency", "error"), model.request_rows),
    "Classes": (("class", "models", "enabled", "aliases"), model.class_rows),
}
TAGS = {  # tab -> row colouring (each table has its own rule)
    "Models": lambda r: {"bad": "bad", "ok": "ok"}.get(r[-1], ""),
    "Requests": lambda r: {"ERR": "bad", "ok": "ok"}.get(r[0], ""),
    "Classes": lambda r: "",
}
# Panels of the older SPA (web-ui/) for which kiko-lite has no endpoint yet: shown as a note only.
MISSING = {"Costs": "costs / monthly cap (old web-ui /api/billing)",
           "Aliases": "alias globs (old web-ui /api/aliases)",
           "Settings": "settings (old web-ui /api/settings)",
           "Responses": "stored responses / text logs (old web-ui /api/responses, /api/logs)"}


def poll(base: str, key: str) -> tuple[dict[str, Any] | None, str]:
    """One blocking round: (status, "") or (None, "down" | "auth")."""
    try:
        return client.fetch_status(base, key), ""
    except client.AuthNeeded:
        return None, "auth"
    except client.Unreachable:
        return None, "down"


def make_icon(root: tk.Misc, color: str, size: int = 16) -> tk.PhotoImage:
    img = tk.PhotoImage(master=root, width=size, height=size)
    r = size / 2 - 0.5
    for y in range(size):
        for x in range(size):
            if (x - r) ** 2 + (y - r) ** 2 <= r * r:
                img.put(color, (x, y))
    return img


class App:
    def __init__(self, root: tk.Tk, base: str, key: str = "") -> None:
        self.root, self.base, self.key = root, base.rstrip("/"), key
        self.q: queue.Queue[tuple[dict[str, Any] | None, str]] = queue.Queue()
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.icons = {s: make_icon(root, c) for s, c in model.COLORS.items()}
        self.trees: dict[str, ttk.Treeview] = {}
        self.has_tray = False
        root.title("kiko-lite")
        root.geometry("1000x500")
        self.banner = tk.StringVar(value="waiting for kiko-lite ...")
        ttk.Label(root, textvariable=self.banner).pack(fill="x", padx=6, pady=3)
        nb = ttk.Notebook(root)
        nb.pack(fill="both", expand=True, padx=6, pady=3)
        self.server_tree = self._tree(nb, "Status", ("field", "value"))
        for name, (cols, _) in TREES.items():
            self.trees[name] = self._tree(nb, name, cols)
        for name, text in MISSING.items():
            f = ttk.Frame(nb)
            ttk.Label(f, text=f"Not available: kiko-lite has no endpoint for {text}.").pack(padx=10, pady=10)
            nb.add(f, text=name)
        for t in (self.server_tree, *self.trees.values()):
            t.tag_configure("bad", foreground="#b91c1c")
            t.tag_configure("ok", foreground="#15803d")
        ttk.Button(root, text="Refresh", command=self.wake.set).pack(side="right", padx=6, pady=3)
        self._setup_tray()
        root.protocol("WM_DELETE_WINDOW", self.close_window)
        threading.Thread(target=self._worker, daemon=True).start()
        root.after(200, self._drain)

    @staticmethod
    def _tree(nb: ttk.Notebook, title: str, cols: tuple[str, ...]) -> ttk.Treeview:
        f = ttk.Frame(nb)
        t = ttk.Treeview(f, columns=cols, show="headings")
        for c in cols:
            t.heading(c, text=c)
            t.column(c, width=110, stretch=True)
        sb = ttk.Scrollbar(f, orient="vertical", command=t.yview)
        t.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        t.pack(fill="both", expand=True)
        nb.add(f, text=title)
        return t

    # ---- worker / queue -----------------------------------------------------------------------
    def _worker(self) -> None:
        while not self.stop.is_set():
            self.q.put(poll(self.base, self.key))
            self.wake.wait(INTERVAL_MS / 1000)
            self.wake.clear()

    def _drain(self) -> None:
        try:
            latest = None
            while True:
                try:
                    latest = self.q.get_nowait()
                except queue.Empty:
                    break
            if latest is not None:
                self.show(*latest)
        finally:
            self.root.after(200, self._drain)

    def show(self, status: dict[str, Any] | None, problem: str) -> None:
        state = model.overall_state(status, problem)
        tip = model.tooltip(state, status, self.base, problem)
        self.banner.set(f"{self.base}  |  {tip}")
        self._tray_update(state, tip)
        if status is None:
            return
        self._fill(self.server_tree, model.server_rows(status), lambda r: "")
        for name, (_, build) in TREES.items():
            self._fill(self.trees[name], build(status), TAGS[name])

    @staticmethod
    def _fill(tree: ttk.Treeview, rows: list[tuple[str, ...]], tag: Any) -> None:
        tree.delete(*tree.get_children())
        for r in rows:
            tree.insert("", "end", values=r, tags=(tag(r),))

    # ---- window / tray ------------------------------------------------------------------------
    def open_window(self) -> None:
        self.root.deiconify()
        self.root.lift()

    def close_window(self) -> None:
        if self.has_tray:
            self.root.withdraw()  # keep living in the tray
        else:
            self.quit()

    def open_web(self) -> None:
        webbrowser.open(self.base + "/ui")

    def quit(self) -> None:
        self.stop.set()
        self.wake.set()
        if self.has_tray:
            try:
                self.root.tk.call("tk", "systray", "destroy")
            except tk.TclError:
                pass
        self.root.destroy()

    def _setup_tray(self) -> None:
        """Tk 9: ``tk systray create|configure|exists|destroy``. Tk 8.6 has none: no tray, window only."""
        try:
            self.root.tk.call("tk", "systray", "exists")
        except tk.TclError:
            return
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Open window", command=self.open_window)
        menu.add_command(label="Open web UI in browser", command=self.open_web)
        menu.add_separator()
        menu.add_command(label="Quit", command=self.quit)
        # kiko-lite has no start/stop/restart endpoint and its CLI only has a foreground `serve`: not offered.

        def popup() -> None:
            menu.tk_popup(self.root.winfo_pointerx(), self.root.winfo_pointery())

        try:
            self.root.tk.call("tk", "systray", "create", "-image", str(self.icons["unknown"]),
                              "-text", "kiko-lite", "-button1", self.root.register(self.open_window),
                              "-button3", self.root.register(popup))
        except tk.TclError:
            return
        self.has_tray = True

    def _tray_update(self, state: str, tip: str) -> None:
        if self.has_tray:
            try:
                self.root.tk.call("tk", "systray", "configure", "-image", str(self.icons[state]), "-text", tip[:127])
            except tk.TclError:
                pass
