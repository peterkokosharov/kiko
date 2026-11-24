# SPDX-License-Identifier: MIT
# Author: Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-5-5
"""One systemd user unit name everywhere: `kiko install`, the zip's unit template and the tray hosts (KIKO_UNIT)."""
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def test_install_command_and_tray_hosts_agree_on_the_unit_name():
    kiko = (REPO / "tray/tools/kiko").read_text(encoding="utf-8")
    assert 'UNIT_BASE="${KIKO_UNIT:-kiko-lite}"' in kiko
    assert (REPO / "gateway/tools/kiko-lite.service").is_file()
    for host in ("tray/gtk3/gtk_host.c", "tray/qt6/qt_host.cpp"):
        text = (REPO / host).read_text(encoding="utf-8")
        assert "KIKO_UNIT" in text and '"kiko-lite"' in text, host
    assert "kiko-proxy" not in (REPO / "tray/qt6/qt_host.cpp").read_text(encoding="utf-8")
