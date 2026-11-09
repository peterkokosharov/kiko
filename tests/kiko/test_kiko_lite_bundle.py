# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""The portable kiko-lite zip: file list and that it runs unpacked (no litellm, no __pycache__, standalone tests)."""
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import kiko_lite_bundle as kb  # noqa: E402

_COMMIT = ["-c", "commit.gpgsign=false", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, check=True).stdout


def _repo_files():
    """Repo paths that go into the bundle, read from the working tree (new files may not be committed yet)."""
    tracked = _git(kb.ROOT, "ls-files", "-z", *kb.PACKAGES, "tests").decode("utf-8").split("\0")
    names = {n for n in tracked if n and kb._is_bundled(n)} | set(kb.SINGLE_FILES) | set(kb.RENAMED)
    names |= set(kb.TEST_EXTRA)
    return sorted(n for n in names if (kb.ROOT / n).is_file())


def _make_repo(tmp_path):
    """Temp git repo holding the bundle files (LF) plus an unrelated file; returns (root, {name: bytes})."""
    root = tmp_path / "repo"
    written = {}
    for f in _repo_files():
        data = (kb.ROOT / f).read_bytes().replace(b"\r\n", b"\n")
        (root / f).parent.mkdir(parents=True, exist_ok=True)
        (root / f).write_bytes(data)
        written[f] = data
    (root / "other.txt").write_text("x\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "-c", "core.autocrlf=false", "add", "-A")
    _git(root, *_COMMIT, "commit", "-q", "-m", "x")
    return root, written


def _refused(fn):
    try:
        fn()
    except kb.BundleError as e:
        return str(e)
    raise AssertionError("expected BundleError")


def test_bundle_contents_and_runs(tmp_path):
    root, _ = _make_repo(tmp_path)
    z = kb.build(tmp_path / "out", root=root, tag="test")
    assert z.name == "kiko-lite-test.zip"
    with zipfile.ZipFile(z) as zf:
        names = zf.namelist()
        zf.extractall(tmp_path / "unpacked")
    assert len(names) == len(set(names))
    for required in ("kiko-lite", "kiko-lite.cmd", "README.md", "LICENSE", "VERSION", "docs/LINUX-INSTALL.md",
                     "tests/kiko/conftest.py", "tests/kiko/test_kiko_lite_app.py", "docs/LINUX-KEYSTORE-TEST.md",
                     "docs/GITHUB-DEPLOY.md", "kiko-lite.service", "install-service.sh",
                     "tools/linux_keystore_check.py", "kiko_lite/__main__.py", "kikolib/keystore.py"):
        assert required in names
    for n in names:
        assert "__pycache__" not in n and not n.endswith(".pyc")
        if n.startswith("tests/"):
            assert kb.is_bundled_test(n) or n == "tests/kiko/conftest.py", n
        else:
            assert "litellm" not in n.lower()
            assert "/tests/" not in n
        assert ".bg." not in n and "/bg/" not in n, n  # Bulgarian docs are read separately, never zipped
        assert n in ("kiko-lite", "kiko-lite.cmd", "README.md", "LICENSE", "VERSION", "kiko-lite.service",
                     "install-service.sh") \
            or n.split("/")[0] in {"kikolib", "kiko_lite", "tools", "docs", "tests"}
    assert "tests/kiko/test_kiko_lite_bundle.py" not in names
    assert "tests/kiko/test_kiko_contract_full_vs_lite.py" not in names
    assert "docs/KIKO-LITE-README.md" not in names and "docs/KIKO-LITE-LICENSE" not in names
    with zipfile.ZipFile(z) as zf:
        assert zf.read("VERSION").decode().startswith("kiko-lite test 20")
        lic = zf.read("LICENSE")
        assert b"LiteLLM Home Edition" not in lic and b"Peter Kokosharov" in lic and b"BerriAI" in lic
        assert zf.read("README.md").lstrip().startswith(b"# kiko-lite")
        sh = zf.read("kiko-lite")
        assert b"command -v python3" in sh and b'"$py" -m kiko_lite' in sh
        assert (zf.getinfo("install-service.sh").external_attr >> 16) & 0o777 == 0o755
        assert b"\r" not in zf.read("install-service.sh") and b"ExecStart=" in zf.read("kiko-lite.service")
        cmd = zf.read("kiko-lite.cmd")
        assert b"py -3" in cmd and b"%*" in cmd and b"\r\n" in cmd
    with zipfile.ZipFile(z) as zf:
        for n in names:
            if n.endswith(".py"):
                assert not kb.imports_litellm(zf.read(n).decode("utf-8")), n

    # -S -E: no site (the venv's editable .pth would load the repo copy) and no PYTHON* variables; the current
    # folder (the unpacked tree) is first on sys.path.
    unpacked = (tmp_path / "unpacked").resolve()
    r = subprocess.run([sys.executable, "-S", "-E", "-c",
                        "import kiko_lite, kikolib; print(kiko_lite.__file__); print(kikolib.__file__)"],
                       cwd=unpacked, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    for line in r.stdout.splitlines():
        assert Path(line).resolve().is_relative_to(unpacked), line

    r = subprocess.run([sys.executable, "-S", "-E", "-m", "kiko_lite", "--help"], cwd=unpacked,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "serve" in r.stdout and "add-model" in r.stdout

    # ALL shipped tests run from the unpacked tree (cwd first on sys.path; no litellm needed); skips are ok
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/kiko"],
                       cwd=unpacked, capture_output=True, text=True, timeout=900)
    assert r.returncode == 0, r.stdout + r.stderr


def test_build_is_deterministic(tmp_path):
    root, _ = _make_repo(tmp_path)
    a = kb.build(tmp_path / "a", root=root, tag="t").read_bytes()
    b = kb.build(tmp_path / "b", root=root, tag="t").read_bytes()
    assert a == b


def test_refuses_litellm(tmp_path):
    root, _ = _make_repo(tmp_path)
    (root / "kiko_lite" / "bad.py").write_text("import litellm\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, *_COMMIT, "commit", "-q", "-m", "bad")
    assert "litellm" in _refused(lambda: kb.build(tmp_path / "o", root=root, tag="x"))


def test_bundle_bytes_come_from_git_not_working_tree(tmp_path):
    """HEAD holds LF, the working tree holds CRLF (hidden from the dirty check by assume-unchanged): zip has LF."""
    root, written = _make_repo(tmp_path)
    _git(root, "update-index", "--assume-unchanged", *written)
    for f, data in written.items():
        (root / f).write_bytes(data.replace(b"\n", b"\r\n"))
    z = kb.build(tmp_path / "o", root=root, tag="x")
    with zipfile.ZipFile(z) as zf:
        for n, data in written.items():
            assert zf.read(kb.dest_name(n)) == data, n
        assert b"\r" not in zf.read("kiko-lite")


def test_refuses_modified_tracked_bundled_file(tmp_path):
    root, _ = _make_repo(tmp_path)
    (root / "kikolib" / "keystore.py").write_bytes(b"# edited\n")
    assert "kikolib/keystore.py" in _refused(lambda: kb.build(tmp_path / "o", root=root, tag="x"))


def test_refuses_deleted_bundled_file(tmp_path):
    root, _ = _make_repo(tmp_path)
    (root / "docs" / "LINUX-INSTALL.md").unlink()
    assert "docs/LINUX-INSTALL.md" in _refused(lambda: kb.build(tmp_path / "o", root=root, tag="x"))


def test_refuses_untracked_file_in_bundled_package(tmp_path):
    root, _ = _make_repo(tmp_path)
    (root / "kiko_lite" / "new_mod.py").write_text("x = 1\n", encoding="utf-8")
    assert "kiko_lite/new_mod.py" in _refused(lambda: kb.build(tmp_path / "o", root=root, tag="x"))


def test_modified_file_outside_bundle_does_not_block(tmp_path):
    root, _ = _make_repo(tmp_path)
    (root / "other.txt").write_text("changed\n", encoding="utf-8")
    (root / "kiko_lite" / "__pycache__").mkdir()
    (root / "kiko_lite" / "__pycache__" / "m.cpython-312.pyc").write_bytes(b"\0")
    z = kb.build(tmp_path / "o", root=root, tag="x")
    with zipfile.ZipFile(z) as zf:
        assert "other.txt" not in zf.namelist()
        assert not any("__pycache__" in n for n in zf.namelist())


def test_real_repo_files_match_git_head():
    """read_git returns exactly `git show HEAD:<path>` for every bundle file, no stray CRLF."""
    try:
        names = kb.file_list()
    except kb.BundleError as e:
        if "missing file" not in str(e) or "at git HEAD" not in str(e):
            raise
        pytest.skip(str(e))  # new bundle files not committed yet
    for n in names:
        blob = _git(kb.ROOT, "show", f"HEAD:{n}")
        got = kb.read_git(kb.ROOT, n)
        assert got == blob, n
        assert b"\r\n" not in got or b"\r\n" in blob, n
    assert len(names) > 10


def test_read_git_rejects_untracked():
    assert _refused(lambda: kb.read_git(kb.ROOT, "no/such/file.txt"))


def test_cmd_launcher_keeps_exit_code_and_scopes_env():
    cmd = kb.WRAPPER_CMD
    assert "setlocal" in cmd and "\n" not in cmd.replace("\r\n", "")
    depth = 0
    for line in cmd.split("\r\n"):
        assert not (depth > 0 and "%errorlevel%" in line.lower()), line  # expands at parse time inside ( )
        depth += line.count("(") - line.count(")")
    assert "%errorlevel%" not in cmd.lower()
    assert "py -3 -m kiko_lite %*" in cmd and "python -m kiko_lite %*" in cmd


def test_cmd_launcher_has_no_trailing_semicolon_when_pythonpath_is_unset():
    cmd = kb.WRAPPER_CMD
    assert 'if defined PYTHONPATH (set "PYTHONPATH=%~dp0;%PYTHONPATH%") else set "PYTHONPATH=%~dp0"' in cmd
    assert "PYTHONPATH=%~dp0;%PYTHONPATH%\"\r\n" not in cmd  # the unconditional form left a trailing semicolon


def test_refuses_modified_shipped_test_file(tmp_path):
    root, _ = _make_repo(tmp_path)
    (root / "tests" / "kiko" / "test_kiko_lite_app.py").write_bytes(b"# edited\n")
    assert "test_kiko_lite_app.py" in _refused(lambda: kb.build(tmp_path / "o", root=root, tag="x"))


@pytest.mark.parametrize("name", ["docs/KIKO-LITE-README.md", "docs/KIKO-LITE-LICENSE"])
def test_refuses_modified_renamed_file(tmp_path, name):
    root, _ = _make_repo(tmp_path)
    (root / name).write_bytes(b"edited\n")
    assert name in _refused(lambda: kb.build(tmp_path / "o", root=root, tag="x"))
