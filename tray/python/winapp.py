# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-5-5
"""pystray tray with the shared contract of the Windows trays (see kiko_tray_core.py).

Icon colour = GET /health on the port (green ok, red down, grey unknown); menu: status, Start chain, Stop chain,
Copy API token, Open web UI, Exit. Start runs apps/kiko/kiko_chain.py as a child, Stop/Exit end only that child (core.ChainOwner). pystray and
Pillow are imported inside the functions, so the logic can be imported without them.
"""
import threading
import webbrowser
from pathlib import Path

import kiko_tray_core as core

INTERVAL_S = 5.0


def make_icon(state: str, size: int = 64):
    """A round coloured badge with a white K (Pillow)."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((1, 1, size - 2, size - 2), fill=core.COLORS.get(state, core.COLORS["unknown"]))
    try:
        font = ImageFont.truetype("arialbd.ttf", int(size * 0.62))
    except OSError:
        font = ImageFont.load_default()
    box = d.textbbox((0, 0), "K", font=font)
    d.text(((size - (box[2] - box[0])) // 2 - box[0], (size - (box[3] - box[1])) // 2 - box[1]), "K",
           fill=(255, 255, 255, 255), font=font)
    return img


def title_for(state: str, port: int, note: str = "") -> str:
    base = {"ok": "kiko ok", "down": "kiko not reachable", "unknown": "kiko: waiting"}.get(state, state)
    return (f"{base} (:{port})" + (f" - {note}" if note else ""))[:127]


class Tray:
    def __init__(self, app_dir: Path | None) -> None:
        self.cfg = core.load_config(app_dir)
        self.token = core.read_token(self.cfg)
        self.state = "unknown"
        self.note = ""
        self.chain = core.ChainOwner(self.cfg)
        self.stop_evt = threading.Event()
        self.icon = None

    # actions (each runs in pystray's callback thread or in a worker)
    def _chain(self, action: str) -> None:
        def work() -> None:
            self.note = f"{action}ing ..." if action == "stop" else "starting ..."
            self.refresh()
            ok, msg = self.chain.run(action)
            self.note = ("" if ok else msg)
            self.poll_once()
        threading.Thread(target=work, daemon=True).start()

    def copy_token(self) -> None:
        self.note = "API token copied" if self.token and core.copy_to_clipboard(self.token) else "no API token"
        self.refresh()

    def open_web(self) -> None:
        webbrowser.open(self.cfg.base + "/ui")

    def exit(self) -> None:
        self.chain.quit()  # ends the chain this tray started (never a foreign one)
        self.stop_evt.set()
        if self.icon is not None:
            self.icon.stop()

    # state
    def poll_once(self) -> None:
        self.state = core.state(self.cfg.port)
        self.refresh()

    def refresh(self) -> None:
        if self.icon is not None:
            self.icon.icon = make_icon(self.state)
            self.icon.title = title_for(self.state, self.cfg.port, self.note)
            self.icon.update_menu()

    def _poller(self) -> None:
        while not self.stop_evt.is_set():
            self.poll_once()
            self.stop_evt.wait(INTERVAL_S)

    def run(self) -> None:
        import pystray as pt

        def menu():
            up = self.state == "ok"
            return (
                pt.MenuItem(title_for(self.state, self.cfg.port), None, enabled=False),
                pt.Menu.SEPARATOR,
                pt.MenuItem("Start chain", lambda i, m: self._chain("start"), enabled=not up),
                pt.MenuItem("Stop chain", lambda i, m: self._chain("stop"), enabled=up),
                pt.Menu.SEPARATOR,
                pt.MenuItem("Copy API token", lambda i, m: self.copy_token(), enabled=bool(self.token)),
                pt.MenuItem("Open web UI", lambda i, m: self.open_web()),
                pt.Menu.SEPARATOR,
                pt.MenuItem("Exit", lambda i, m: self.exit()),
            )

        self.icon = pt.Icon("kiko", make_icon("unknown"), title_for("unknown", self.cfg.port), pt.Menu(menu))
        threading.Thread(target=self._poller, daemon=True).start()
        self.icon.run()
