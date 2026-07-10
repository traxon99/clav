"""Automated architecture rules that aren't (or can't yet be) expressed as ruff/mypy
config. This is the "review rule" 1.3 asks for: no module reads wall-clock time
directly — everything goes through the injected Clock (see src/clav/clock.py).
"""

from __future__ import annotations

import re
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "clav"

# Matches direct wall-clock reads: datetime.now(...), datetime.utcnow(), time.time(),
# time.monotonic(), time.perf_counter(). Word-boundaried so e.g. "self.now()" (calling
# the injected Clock) is not flagged.
_WALLCLOCK_CALL = re.compile(
    r"\b(datetime\.(now|utcnow)|time\.(time|monotonic|monotonic_ns|perf_counter))\s*\("
)

_ALLOWED_FILES = {SRC_ROOT / "clock.py"}


def test_no_wallclock_reads_outside_clock_module() -> None:
    offenders: list[str] = []
    for path in SRC_ROOT.rglob("*.py"):
        if path in _ALLOWED_FILES:
            continue
        text = path.read_text()
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _WALLCLOCK_CALL.search(line):
                offenders.append(f"{path.relative_to(SRC_ROOT)}:{lineno}: {line.strip()}")

    assert not offenders, (
        "Found direct wall-clock reads outside clock.py — inject a Clock instead:\n"
        + "\n".join(offenders)
    )
