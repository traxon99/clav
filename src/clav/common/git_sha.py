"""Resolve the running code's git SHA for reproducibility (Story 4.4,
docs/10-observability.md §5). Reads ``.git`` metadata directly — no
subprocess, so it works in minimal deployment images without a ``git``
binary — with an env override for contexts where ``.git`` isn't present
(e.g. some container builds) and a safe fallback so a missing SHA never
blocks a cycle.
"""

from __future__ import annotations

import os
from pathlib import Path

UNKNOWN_SHA = "unknown"

_REPO_ROOT = Path(__file__).resolve().parents[3]


def resolve_git_sha(repo_root: Path | None = None) -> str:
    override = os.environ.get("CLAV_GIT_SHA")
    if override:
        return override

    git_dir = (repo_root or _REPO_ROOT) / ".git"
    try:
        head = (git_dir / "HEAD").read_text().strip()
    except OSError:
        return UNKNOWN_SHA

    if not head.startswith("ref:"):
        return head or UNKNOWN_SHA

    ref_path = head.split(" ", 1)[1].strip()
    try:
        return (git_dir / ref_path).read_text().strip()
    except OSError:
        pass

    try:
        for line in (git_dir / "packed-refs").read_text().splitlines():
            if line.endswith(ref_path):
                return line.split()[0]
    except OSError:
        pass
    return UNKNOWN_SHA
