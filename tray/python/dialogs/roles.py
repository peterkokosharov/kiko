# Author: Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-4-6
"""tray/dialogs/roles.py — roles dialog data + actions (wraps tray/roles.py)."""
from tray.roles import (
    ENV_VARS,
    ROLE_ALIASES,
    ROLES,
    assign_role,
    env_lines,
    load_roles,
)
from tray.roles import clear_role as _clear_role


def get_roles_data() -> dict:
    """Return role assignments plus metadata needed by the dialog.

    Keys:
      assignments — dict[role, model|None] from load_roles()
      aliases     — ROLE_ALIASES constant
      env_vars    — ENV_VARS constant
      roles       — ordered ROLES list
    """
    return {
        "assignments": load_roles(),
        "aliases": ROLE_ALIASES,
        "env_vars": ENV_VARS,
        "roles": ROLES,
    }


def set_role(role: str, model: str) -> None:
    """Assign *model* to *role* and persist."""
    assign_role(role, model)


def clear_role(role: str) -> None:
    """Clear the assignment for *role* (sets to None)."""
    _clear_role(role)


def get_env_lines() -> list[str]:
    """Return 'export VAR=model' lines for all non-None role assignments."""
    return env_lines(load_roles())
