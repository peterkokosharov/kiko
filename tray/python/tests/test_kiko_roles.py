# Author: Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-4-6
"""tests/test_kiko_roles.py — tests for tray/roles.py."""
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

import tray.roles as roles_mod
from tray.roles import (
    ROLE_ALIASES,
    ROLES,
    ENV_VARS,
    assign_role,
    clear_role,
    env_lines,
    load_roles,
    roles_path,
    save_roles,
    to_kiko_aliases,
)


@pytest.fixture(autouse=True)
def isolate_roles(tmp_path, monkeypatch):
    """Redirect roles_path() to a tmp directory for every test."""
    kiko_dir = tmp_path / ".config" / "kiko"
    kiko_dir.mkdir(parents=True)

    def _fake_roles_path():
        kiko_dir.mkdir(parents=True, exist_ok=True)
        return kiko_dir / "roles.json"

    monkeypatch.setattr(roles_mod, "roles_path", _fake_roles_path)
    yield


# R1: load_roles() returns defaults when file is missing
def test_r1_load_roles_defaults_when_missing():
    result = load_roles()
    for role in ROLES:
        assert result[role] == ROLE_ALIASES[role], (
            f"Expected default {ROLE_ALIASES[role]!r} for role {role!r}, got {result[role]!r}"
        )


# R2: assign_role persists — load_roles returns the saved model
def test_r2_assign_role_persists():
    assign_role("workhorse", "claude-sonnet-4-5")
    result = load_roles()
    assert result["workhorse"] == "claude-sonnet-4-5"


# R3: assign_role with unknown role raises ValueError
def test_r3_assign_unknown_role_raises():
    with pytest.raises(ValueError, match="Unknown role"):
        assign_role("unknown", "x")


# R4: clear_role sets role→None, overriding the default alias
def test_r4_clear_role_sets_none():
    clear_role("thinker")
    result = load_roles()
    assert result["thinker"] is None


# R5: env_lines — workhorse present, subagent=None excluded
def test_r5_env_lines_subagent_none_excluded():
    assignments = {"workhorse": "claude-sonnet-4-5", "subagent": None}
    lines = env_lines(assignments)
    assert len(lines) == 1
    assert "ANTHROPIC_DEFAULT_SONNET_MODEL=claude-sonnet-4-5" in lines[0]
    assert not any("CLAUDE_CODE_SUBAGENT_MODEL" in line for line in lines)


# R6: env_lines — subagent set → CLAUDE_CODE_SUBAGENT_MODEL included
def test_r6_env_lines_subagent_included_when_set():
    assignments = {"subagent": "claude-haiku-4"}
    lines = env_lines(assignments)
    assert any("CLAUDE_CODE_SUBAGENT_MODEL=claude-haiku-4" in line for line in lines)


# R7: to_kiko_aliases maps alias key → model, skips None
def test_r7_to_kiko_aliases_skips_none():
    assignments = {"laborer": "claude-haiku-4", "workhorse": None}
    result = to_kiko_aliases(assignments)
    assert result == {"claude-haiku-latest": "claude-haiku-4"}
    assert "claude-sonnet-latest" not in result


# R8: atomic save — if os.replace raises, old file is unchanged
def test_r8_atomic_save_old_file_unchanged(tmp_path, monkeypatch):
    # Write an initial roles.json via save_roles
    initial = {"workhorse": "original-model"}
    save_roles(initial)

    # Confirm it saved correctly
    assert load_roles()["workhorse"] == "original-model"

    # Now mock os.replace inside roles_mod to raise
    original_replace = os.replace

    def _fail_replace(src, dst):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(roles_mod.os, "replace", _fail_replace)

    with pytest.raises(OSError, match="simulated disk failure"):
        save_roles({"workhorse": "new-model"})

    # Restore os.replace so we can read the file
    monkeypatch.setattr(roles_mod.os, "replace", original_replace)

    # Original file must still hold the old data
    result = load_roles()
    assert result["workhorse"] == "original-model"
