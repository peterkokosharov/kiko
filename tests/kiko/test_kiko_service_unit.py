# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""The systemd user unit template and install-service.sh (repo files in tools/, shipped at the zip root)."""
import configparser
import os
import shutil
import subprocess
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[2] / "tools"
UNIT = TOOLS / "kiko-lite.service"
SCRIPT = TOOLS / "install-service.sh"


def test_unit_parses_and_has_required_keys():
    cp = configparser.RawConfigParser(strict=False)
    cp.optionxform = str
    cp.read_string(UNIT.read_text(encoding="utf-8"))
    svc = cp["Service"]
    assert "serve" in svc["ExecStart"] and "kiko-lite" in svc["ExecStart"]
    assert "@KIKO_DIR_Q@" in svc["ExecStart"]
    assert "--host" not in svc["ExecStart"] and "--port" not in svc["ExecStart"]  # serve reads kiko.toml
    assert svc["Restart"] == "on-failure" and svc["RestartSec"] == "5"
    assert svc["Type"] == "notify"  # serve sends READY=1
    # kiko-lite reads ~/.kiko/.env itself (hot reload); systemd env vars would always win over the file
    assert "EnvironmentFile" not in UNIT.read_text(encoding="utf-8").replace("No EnvironmentFile", "")
    assert cp["Install"]["WantedBy"] == "default.target"
    assert "network-online" not in UNIT.read_text(encoding="utf-8")
    assert UNIT.read_text(encoding="utf-8").startswith("# SPDX-License-Identifier: MIT\n# Copyright (c) 2026 Peter Kokosharov")


def test_files_are_lf_only():
    for f in (UNIT, SCRIPT):
        assert b"\r" not in f.read_bytes(), f.name


def test_script_has_uninstall_and_no_secret_values():
    text = SCRIPT.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh\n") and "set -e" in text and "--uninstall" in text
    assert "enable-linger" in text and "enable --now" not in text


FAKE_LAUNCHER = """#!/bin/sh
cfg="${KIKO_CONFIG_DIR:-$HOME/.kiko}"
if [ "$1 $2" = "config --get" ]; then
    case "$3" in
        config_dir) echo "$cfg" ;;
        env_path) echo "$cfg/.env" ;;
        database_path) echo "$cfg/kiko.db" ;;
        proxy_host) echo 127.0.0.1 ;;
        toml_path) echo "$cfg/settings-file" ;;
        proxy_port) echo 4999 ;;
    esac
fi
exit 0
"""


def _sandbox(tmp_path, dirname="kiko dir"):
    """Unpack dir (named dirname) with the script, unit and a fake launcher; fake systemctl logging its args."""
    d = tmp_path / dirname
    d.mkdir()
    for f in (SCRIPT, UNIT):
        (d / f.name).write_bytes(f.read_bytes())
    # a fake launcher that answers `config --get KEY` the way kiko_lite does (paths from KIKO_CONFIG_DIR or HOME)
    (d / "kiko-lite").write_text(FAKE_LAUNCHER, encoding="utf-8")
    (d / "kiko-lite").chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "systemctl.log"
    sc = bin_dir / "systemctl"
    sc.write_text(f'#!/bin/sh\necho "$*" >> "{log.as_posix()}"\nexit 0\n', encoding="utf-8")
    sc.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    return d, bin_dir, log, home


def _run(d, bin_dir, home, *args):
    env = {k: v for k, v in os.environ.items() if k not in ("XDG_CONFIG_HOME", "KIKO_CONFIG_DIR")}
    env["PATH"] = bin_dir.as_posix() + os.pathsep + env.get("PATH", "")
    env["HOME"] = home.as_posix()
    return subprocess.run(["sh", (d / "install-service.sh").as_posix(), *args], capture_output=True, text=True,
                          env=env, timeout=60)


@pytest.mark.skipif(shutil.which("sh") is None, reason="no sh")
@pytest.mark.parametrize("dirname", ["kiko dir", "plain"])
def test_install_and_uninstall_with_fake_systemctl(tmp_path, dirname):
    d, bin_dir, log, home = _sandbox(tmp_path, dirname)
    r = _run(d, bin_dir, home)
    assert r.returncode == 0, r.stderr
    unit = home / ".config" / "systemd" / "user" / "kiko-lite.service"
    text = unit.read_text(encoding="utf-8")
    assert "@KIKO_DIR" not in text
    exec_line = next(ln for ln in text.splitlines() if ln.startswith("ExecStart="))
    wd_line = next(ln for ln in text.splitlines() if ln.startswith("WorkingDirectory="))
    assert exec_line.startswith('ExecStart="/') or os.name == "nt"  # (Git sh on Windows: /c/... style)
    assert exec_line.endswith(f'/{dirname}/kiko-lite" serve --no-auth --rss-limit-mb 1024')
    assert not any(ln.startswith('Environment="KIKO_CONFIG_DIR') for ln in text.splitlines())  # default folder
    assert wd_line.endswith(f"/{dirname}") and exec_line.split('"')[1] == wd_line.split("=", 1)[1] + "/kiko-lite"
    if os.name == "posix":
        assert wd_line == f"WorkingDirectory={d}"
    calls = log.read_text(encoding="utf-8").splitlines()
    assert "--user daemon-reload" in calls and "--user enable kiko-lite" in calls
    assert "--user restart kiko-lite" in calls
    assert not any("enable --now" in c for c in calls)
    env_file = home / ".kiko" / ".env"
    assert env_file.read_text(encoding="utf-8").startswith("# API keys")
    if os.name == "posix":
        assert (env_file.stat().st_mode & 0o777) == 0o600
    # a re-run never overwrites an existing .env, and restarts again
    env_file.write_text("GEMINI_API_KEY=keep\n", encoding="utf-8")
    log.write_text("", encoding="utf-8")
    assert _run(d, bin_dir, home).returncode == 0
    assert env_file.read_text(encoding="utf-8") == "GEMINI_API_KEY=keep\n"
    assert "--user restart kiko-lite" in log.read_text(encoding="utf-8").splitlines()
    # uninstall
    log.write_text("", encoding="utf-8")
    r = _run(d, bin_dir, home, "--uninstall")
    assert r.returncode == 0, r.stderr
    assert not unit.exists()
    calls = log.read_text(encoding="utf-8").splitlines()
    assert "--user disable --now kiko-lite" in calls and "--user daemon-reload" in calls
    assert env_file.exists()


@pytest.mark.skipif(shutil.which("sh") is None, reason="no sh")
def test_a_custom_config_dir_goes_into_the_unit_and_the_env_file_lives_there(tmp_path):
    d, bin_dir, log, home = _sandbox(tmp_path)
    cfg = tmp_path / "my cfg"
    env = {k: v for k, v in os.environ.items() if k not in ("XDG_CONFIG_HOME", "KIKO_CONFIG_DIR")}
    env.update(PATH=bin_dir.as_posix() + os.pathsep + env.get("PATH", ""), HOME=home.as_posix(),
               KIKO_CONFIG_DIR=cfg.as_posix())
    r = subprocess.run(["sh", (d / "install-service.sh").as_posix()], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    text = (home / ".config" / "systemd" / "user" / "kiko-lite.service").read_text(encoding="utf-8")
    assert f'Environment="KIKO_CONFIG_DIR={cfg.as_posix()}"' in text
    assert (cfg / ".env").is_file() and not (home / ".kiko").exists()
    assert "http://127.0.0.1:4999" in r.stdout  # the port the launcher reported, not a guess


@pytest.mark.skipif(shutil.which("sh") is None, reason="no sh")
def test_install_stops_when_the_launcher_cannot_report_its_config(tmp_path):
    d, bin_dir, log, home = _sandbox(tmp_path)
    (d / "kiko-lite").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    r = _run(d, bin_dir, home)
    assert r.returncode == 1 and "cannot read the configuration" in r.stderr and "enable" not in log.read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("sh") is None, reason="no sh")
@pytest.mark.parametrize("args", [("--bogus",), ("--uninstall", "x"), ("a", "b"), ("",)])
def test_unknown_arguments_exit_2(tmp_path, args):
    d, bin_dir, log, home = _sandbox(tmp_path)
    r = _run(d, bin_dir, home, *args)
    assert r.returncode == 2 and "usage" in r.stderr
    assert not log.exists()


@pytest.mark.skipif(shutil.which("sh") is None, reason="no sh")
def test_script_syntax_ok():
    r = subprocess.run(["sh", "-n", str(SCRIPT)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
