# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""TASK #124 review fixes: the log file stays inside the config dir, writer robustness, validation limits."""
import importlib.util
import logging
import os
import tomllib
from pathlib import Path

import pytest

from kikolib import config, logs
from kikolib import tomlwrite as tw

from .test_web_settings import env  # noqa: F401  (a logged-in web client fixture)

BS = chr(92)
SEPS = (chr(0x85), chr(0x2028), chr(0x2029))


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    d = tmp_path / "cfg"
    d.mkdir()
    monkeypatch.setenv("KIKO_CONFIG_DIR", str(d))
    return d


@pytest.fixture
def clean_root_handlers():
    root = logging.getLogger()
    before = list(root.handlers)
    yield root
    for h in list(root.handlers):
        if h not in before:
            root.removeHandler(h)
            h.close()


def write_toml(cfg, text):
    (cfg / "kiko.toml").write_text(text, encoding="utf-8")


# ---- blocker: [logs] file ----
BAD_PATHS = ["../x.log", "a/../../x.log", "..", "/etc/x.log", "C:/x.log", "c:x.log", BS + "x.log", "C:" + BS + "x.log",
             BS + BS + "server" + BS + "share" + BS + "x.log", "a" + BS + ".." + BS + ".." + BS + "x.log"]


@pytest.mark.parametrize("bad", BAD_PATHS)
def test_hand_edited_log_file_outside_the_config_dir_is_unset(cfg, bad, caplog, clean_root_handlers):
    write_toml(cfg, "[logs]\nfile = '" + bad + "'\n")
    with caplog.at_level(logging.WARNING):
        assert config.log_file() is None and logs.setup_logging() is None
    assert "inside the config folder" in caplog.text and "logs.file" in caplog.text
    assert not (cfg.parent / "x.log").exists()


@pytest.mark.parametrize("bad", BAD_PATHS)
def test_parse_value_refuses_them(bad):
    with pytest.raises(ValueError):
        config.parse_value(config.BY_NAME["logs.file"], bad)


def test_log_file_inside_is_fine_and_a_symlink_out_is_refused(cfg, tmp_path):
    write_toml(cfg, '[logs]\nfile = "sub/dir/k.log"\n')
    assert config.log_file() == cfg / "sub" / "dir" / "k.log"
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (cfg / "link").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("cannot create a symlink here")
    write_toml(cfg, '[logs]\nfile = "link/k.log"\n')
    assert config.log_file() is None


def test_config_json_shows_no_log_file_for_a_bad_value(cfg):
    write_toml(cfg, "[logs]\nfile = '../x.log'\n")
    assert config.resolved()["log_file"] == ""


@pytest.mark.parametrize("bad", ["../x.log", "../../x.log", "/tmp/x.log", "C:/x.log", "C:" + BS + "x.log",
                                 BS + BS + "h" + BS + "s" + BS + "x.log"])
def test_form_refuses_a_log_file_outside_the_config_dir(env, bad):
    c, *_ = env
    resp, html = c.post("/settings/save", {"logs.file": bad})
    assert resp.status == 400 and "flash error" in html
    assert not config.toml_path().exists()
    assert not (config.config_dir().parent / "x.log").exists() and not (config.config_dir() / "x.log").exists()


def test_form_accepts_a_relative_log_file_and_the_retention_limit(env):
    c, *_ = env
    assert c.post("/settings/save", {"logs.file": "logs/ok.log", "logs.retention_days": "3650", "logs.store_text": "on"})[0].status == 303
    assert c.post("/settings/save", {"logs.retention_days": "3651", "logs.store_text": "on"})[0].status == 400
    data = tomllib.loads(config.toml_path().read_text(encoding="utf-8"))
    assert data["logs"] == {"file": "logs/ok.log", "retention_days": 3650}


# ---- nits ----
def test_ipv6_host_is_saved_without_brackets(env):
    c, *_ = env
    assert config.parse_value(config.BY_NAME["proxy.host"], "[::1]") == "::1"
    assert c.post("/settings/save", {"web.host": "[::1]"})[0].status == 303
    assert tomllib.loads(config.toml_path().read_text(encoding="utf-8"))["web"]["host"] == "::1"


def test_line_separator_values_are_refused_and_do_not_brick_saves(env):
    c, *_ = env
    for sep in SEPS:
        assert c.post("/settings/save", {"health.no_probe": "a" + sep + "b"})[0].status == 400
    assert not config.toml_path().exists()
    # a hand-edited file that already holds one must not brick the writer either
    config.toml_path().write_text('[health]\nno_probe = "a' + SEPS[1] + 'b"\n[logs]\nretention_days = 1\n',
                                  encoding="utf-8")
    assert c.post("/settings/save", {"logs.retention_days": "2"})[0].status == 303
    data = tomllib.loads(config.toml_path().read_text(encoding="utf-8"))
    assert data["logs"]["retention_days"] == 2


def test_writer_edits_a_file_that_contains_line_separators():
    text = '[a]\nv = "x' + "".join(SEPS) + 'y"\n[b]\nk = 1\n'
    out = tw.update_text(text, {("b", "k"): 2, ("a", "n"): 5})
    data = tomllib.loads(out)
    assert data["a"]["v"] == "x" + "".join(SEPS) + "y" and data["b"]["k"] == 2 and data["a"]["n"] == 5
    with pytest.raises(tw.TomlWriteError):
        tw.update_text("", {("a", "b"): "x" + SEPS[1]})


def test_replace_retries_on_permission_error_then_gives_a_clear_error(tmp_path, monkeypatch):
    p = tmp_path / "kiko.toml"
    p.write_text("[a]\nb = 1\n", encoding="utf-8")
    real, calls = os.replace, []

    def flaky(a, b):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError("in use")
        real(a, b)
    monkeypatch.setattr(os, "replace", flaky)
    monkeypatch.setattr(tw.time, "sleep", lambda s: None)
    tw.set_value(p, "a", "b", 2)
    assert len(calls) == 3 and tomllib.loads(p.read_text(encoding="utf-8"))["a"]["b"] == 2
    monkeypatch.setattr(os, "replace", lambda a, b: (_ for _ in ()).throw(PermissionError("in use")))
    with pytest.raises(tw.TomlWriteError, match="open in another program"):
        tw.set_value(p, "a", "b", 3)
    assert tomllib.loads(p.read_text(encoding="utf-8"))["a"]["b"] == 2
    assert [f.name for f in tmp_path.iterdir()] == ["kiko.toml"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_file_mode_is_kept(tmp_path):
    p = tmp_path / "kiko.toml"
    p.write_text("[a]\nb = 1\n", encoding="utf-8")
    os.chmod(p, 0o644)
    tw.set_value(p, "a", "b", 2)
    assert (p.stat().st_mode & 0o777) == 0o644


def test_cap_zero_means_no_cap_and_the_help_says_so():
    assert "0 means no cap" in config.BY_NAME["billing.monthly_cap_usd"].help
    assert config.parse_value(config.BY_NAME["billing.monthly_cap_usd"], "0") == 0.0


def test_web_refuses_to_start_on_a_non_loopback_host_from_the_file_with_the_default_password(cfg, tmp_path, capsys,
                                                                                             monkeypatch):
    import web.__main__ as web_main
    write_toml(cfg, '[web]\nhost = "0.0.0.0"\n')
    monkeypatch.setattr(web_main.kserver, "serve", lambda *a, **k: pytest.fail("must not serve"))
    assert web_main.main(["--db", str(tmp_path / "w.db")]) == 2
    assert "refusing to listen on 0.0.0.0" in capsys.readouterr().err


def test_old_web_ui_says_what_is_wrong_when_the_port_is_busy(cfg, tmp_path, capsys, monkeypatch):
    path = Path(__file__).resolve().parents[2] / "web-ui" / "main.py"
    spec = importlib.util.spec_from_file_location("kiko_webui_main_busy", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod.kserver, "serve", lambda *a, **k: (_ for _ in ()).throw(OSError(10048, "Address in use")))
    assert mod.main(["--db", str(tmp_path / "o.db")]) == 1
    err = capsys.readouterr().err
    assert "shares the [web] port" in err and "--port" in err
