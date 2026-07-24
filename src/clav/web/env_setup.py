"""Reading/writing specific keys in the ``.env`` credentials file, so an
operator can paste in Alpaca paper keys from the dashboard instead of SSHing
in to hand-edit the file. Secrets stay ``.env``-only, exactly as everywhere
else in this project (docs/06-safety-and-risk.md) -- this never touches the
DB or ``config.yaml``, it only rewrites ``.env`` in place. A written key only
takes effect on the next ``clav-core``/``clav-web`` restart: pydantic-settings
reads ``.env`` once, at process startup (``clav.config.load_settings``).
"""

from __future__ import annotations

import re
from pathlib import Path

from clav.config import DEFAULT_ENV_FILE

__all__ = ["DEFAULT_ENV_FILE", "env_key_is_set", "write_env_values"]

_ENV_KEY_LINE = re.compile(r"^\s*#?\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")


def env_key_is_set(path: Path, key: str) -> bool:
    """True if ``key`` has a non-empty value on an active (non-commented)
    line. Never returns or logs the value itself."""
    if not path.exists():
        return False
    prefix = f"{key}="
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix):
            return bool(stripped[len(prefix) :].strip())
    return False


def write_env_values(path: Path, values: dict[str, str]) -> None:
    """Set ``values`` in the ``.env`` file at ``path``: updates an existing
    line for a key (uncommenting it if it was a commented-out example, as in
    ``.env.example``) in place, preserving every other line untouched, or
    appends a new line if the key isn't present yet. Creates the file if
    missing. Restricts permissions to 0600 (secrets).

    Raises ``ValueError`` if any value contains a newline -- otherwise a
    crafted value could inject an unrelated extra line into the file.
    """
    for value in values.values():
        if "\n" in value or "\r" in value:
            raise ValueError("value may not contain a newline")

    remaining = dict(values)
    lines = path.read_text().splitlines() if path.exists() else []
    new_lines: list[str] = []
    for line in lines:
        match = _ENV_KEY_LINE.match(line)
        key = match.group(1) if match else None
        if key is not None and key in remaining:
            new_lines.append(f"{key}={remaining.pop(key)}")
        else:
            new_lines.append(line)
    for key, value in remaining.items():
        new_lines.append(f"{key}={value}")

    path.write_text("\n".join(new_lines) + "\n")
    path.chmod(0o600)
