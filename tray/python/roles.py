# Author: Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-4-6
"""tray/roles.py — role→model assignment logic for kiko-tray."""
import json
import os
import subprocess
from pathlib import Path

ROLES = ["laborer", "workhorse", "thinker", "auditor", "subagent"]

ROLE_ALIASES = {
    "laborer":   "claude-haiku-latest",
    "workhorse": "claude-sonnet-latest",
    "thinker":   "claude-opus-latest",
    "auditor":   "claude-fable-latest",
    "subagent":  None,  # no default
}

ENV_VARS = {
    "laborer":   "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "workhorse": "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "thinker":   "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "auditor":   "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "subagent":  "CLAUDE_CODE_SUBAGENT_MODEL",
}


def roles_path() -> Path:
    """Return path to roles.json; creates ~/.config/kiko/ if needed."""
    p = Path.home() / ".config" / "kiko"
    p.mkdir(parents=True, exist_ok=True)
    return p / "roles.json"


def load_roles() -> dict[str, str | None]:
    """Return current role assignments; missing keys default to ROLE_ALIASES[role]."""
    path = roles_path()
    stored: dict[str, str | None] = {}
    if path.exists():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    result: dict[str, str | None] = {}
    for role in ROLES:
        if role in stored:
            result[role] = stored[role]
        else:
            result[role] = ROLE_ALIASES[role]
    return result


def save_roles(assignments: dict[str, str | None]) -> None:
    """Write roles.json atomically (write to .tmp, then rename)."""
    path = roles_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(assignments, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def assign_role(role: str, model: str | None) -> None:
    """Assign a model to a role and persist. Raises ValueError on unknown role."""
    if role not in ROLES:
        raise ValueError(f"Unknown role {role!r}. Valid roles: {ROLES}")
    assignments = load_roles()
    assignments[role] = model
    save_roles(assignments)


def clear_role(role: str) -> None:
    """Set role→None (overrides default alias)."""
    assign_role(role, None)


def env_lines(assignments: dict[str, str | None]) -> list[str]:
    """Return list of 'export VAR=model' lines for non-None assignments.

    Subagent is only included when explicitly set (not None).
    """
    lines: list[str] = []
    for role in ROLES:
        model = assignments.get(role)
        if model is None:
            continue
        var = ENV_VARS[role]
        lines.append(f"export {var}={model}")
    return lines


def to_kiko_aliases(assignments: dict[str, str | None]) -> dict[str, str]:
    """Convert role assignments to kiko's model_aliases dict.

    Maps ROLE_ALIASES[role] (e.g. 'claude-haiku-latest') → assigned model name.
    Skips None assignments and roles with no alias key.
    """
    result: dict[str, str] = {}
    for role, model in assignments.items():
        if model is None:
            continue
        alias_key = ROLE_ALIASES.get(role)
        if alias_key is None:
            continue
        result[alias_key] = model
    return result


def list_discovered_models(kiko_executable: str = "kiko") -> list[dict]:
    """Run `kiko --list-models`, parse JSON output. Returns [] on any failure.

    This is a stub — kiko --list-models doesn't exist yet in the server.
    """
    try:
        result = subprocess.run(
            [kiko_executable, "--list-models"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return []
        return json.loads(result.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired,
            json.JSONDecodeError, OSError):
        return []
