# Author: Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-4-6
"""tray/dialogs/keys.py — key storage dialog data + actions.

SECURITY RULES:
  - Key values are NEVER stored in any data structure, logged, or returned.
  - KeyEntry.exists is a boolean flag only.
  - set_key() writes directly to the key file; the value is not retained.

Storage (current implementation): file-based at ~/.config/kiko/{provider}.key
with mode 0600.  Secret Service (secret-tool) is reserved for a future path.
"""
import os
import stat
from dataclasses import dataclass
from pathlib import Path

_PROVIDERS = ("anthropic", "gemini", "openai")
_KEYS_DIR = Path.home() / ".config" / "kiko"


def _key_path(provider: str, keys_dir: Path | None = None) -> Path:
    d = keys_dir if keys_dir is not None else _KEYS_DIR
    return d / f"{provider}.key"


@dataclass
class KeyEntry:
    provider: str   # "anthropic", "gemini", "openai"
    exists: bool    # True if key file is present and non-empty
    source: str     # "file" | "env" | "secret-service" | "none"
    # Key value is intentionally absent from this dataclass.


def list_keys(keys_dir: Path | None = None) -> list[KeyEntry]:
    """Return KeyEntry list for all known providers.

    Checks (in order): env var, key file.  Never returns key values.
    """
    entries: list[KeyEntry] = []
    env_var_map = {
        "anthropic": "ANTHROPIC_API_KEY",
        "gemini": "GEMINI_API_KEY",
        "openai": "OPENAI_API_KEY",
    }
    for provider in _PROVIDERS:
        env_var = env_var_map.get(provider, "")
        if os.environ.get(env_var):
            entries.append(KeyEntry(provider=provider, exists=True, source="env"))
            continue
        path = _key_path(provider, keys_dir)
        if path.exists() and path.stat().st_size > 0:
            entries.append(KeyEntry(provider=provider, exists=True, source="file"))
        else:
            entries.append(KeyEntry(provider=provider, exists=False, source="none"))
    return entries


def set_key(provider: str, value: str, keys_dir: Path | None = None) -> None:
    """Store the API key for *provider* in a 0600 file.  Value is never logged."""
    if provider not in _PROVIDERS:
        raise ValueError(f"Unknown provider {provider!r}. Valid: {_PROVIDERS}")
    d = keys_dir if keys_dir is not None else _KEYS_DIR
    d.mkdir(parents=True, exist_ok=True)
    path = _key_path(provider, keys_dir)
    # Write with restricted permissions; value is written atomically via tmp file.
    tmp = path.with_suffix(".tmp")
    tmp.write_text(value, encoding="utf-8")
    try:
        tmp.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except NotImplementedError:
        pass  # Windows — skip chmod; file permissions handled by NTFS ACL
    os.replace(tmp, path)


def delete_key(provider: str, keys_dir: Path | None = None) -> None:
    """Remove the stored key file for *provider* if it exists."""
    if provider not in _PROVIDERS:
        raise ValueError(f"Unknown provider {provider!r}. Valid: {_PROVIDERS}")
    path = _key_path(provider, keys_dir)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
