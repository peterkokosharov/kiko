# Author: Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-4-6
"""tray/logs.py — log tail and key redaction for kiko-tray."""
import os
import re
from pathlib import Path

# Patterns to redact.
# For patterns with groups: group 1 is the "keep" prefix, optional group 2
# is a trailing character to preserve (e.g. closing quote).
# The sk- pattern has no groups — the whole match is the secret.
_REDACT_PATTERNS = [
    re.compile(r'(?i)(authorization:\s*bearer\s+)\S+'),
    re.compile(r'(?i)(x-api-key:\s*)\S+'),
    re.compile(r'(?i)(x-goog-api-key:\s*)\S+'),
    re.compile(r'(?i)("api_key"\s*:\s*")[^"]+("?)'),
    re.compile(r'(?i)(api_key=)[^&\s"]+'),
    re.compile(r'(?i)sk-[a-zA-Z0-9\-]{8,}'),  # OpenAI-style keys — no group
]


def redact(text: str) -> str:
    """Replace sensitive values with [REDACTED].

    The key name / header name is kept; only the value is replaced.
    Examples::

        redact("Authorization: Bearer sk-abc123")
        # -> "Authorization: Bearer [REDACTED]"

        redact('{"api_key": "sk-secret"}')
        # -> '{"api_key": "[REDACTED]"}'
    """
    for pattern in _REDACT_PATTERNS:
        groups = pattern.groups
        if groups == 0:
            # Whole-match replacement (e.g. bare sk- key)
            text = pattern.sub('[REDACTED]', text)
        elif groups == 1:
            # Keep group 1 prefix, redact the rest
            text = pattern.sub(lambda m: m.group(1) + '[REDACTED]', text)
        else:
            # Keep group 1 prefix, redact value, keep group 2 suffix
            text = pattern.sub(
                lambda m: m.group(1) + '[REDACTED]' + (m.group(2) or ''),
                text,
            )
    return text


def tail_file(path: Path, n: int = 100) -> list[str]:
    """Return last *n* lines of a file.

    Returns an empty list if the file does not exist.
    """
    if not path.exists():
        return []
    with open(path, encoding='utf-8', errors='replace') as fh:
        lines = fh.readlines()
    return [line.rstrip('\n') for line in lines[-n:]]


def get_log_folder() -> Path:
    """Return the kiko log folder (~/.kircho/logs/ or $KIKO_LOG_DIR)."""
    env = os.environ.get('KIKO_LOG_DIR')
    if env:
        return Path(env)
    return Path.home() / '.kircho' / 'logs'


def server_log_path() -> Path:
    """Absolute path to the kiko server log file."""
    return get_log_folder() / 'kiko-server.log'


def tray_log_path() -> Path:
    """Absolute path to the kiko tray log file."""
    return get_log_folder() / 'kiko-tray.log'


def get_server_log(n: int = 100) -> list[str]:
    """Last *n* lines of the server log, with secrets redacted."""
    return [redact(line) for line in tail_file(server_log_path(), n)]


def get_tray_log(n: int = 100) -> list[str]:
    """Last *n* lines of the tray log, with secrets redacted."""
    return [redact(line) for line in tail_file(tray_log_path(), n)]


def copy_last_n(n: int = 100) -> str:
    """Combined last *n* lines from both logs, redacted, as a single string."""
    server = get_server_log(n)
    tray = get_tray_log(n)
    lines = server + tray
    return '\n'.join(lines[-n:])
