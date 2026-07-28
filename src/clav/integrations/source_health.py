"""Make a fail-open source's failure visible instead of silent.

Every news/social/discovery adapter swallows exceptions and returns ``[]`` so a
dead source can never abort a scan cycle. That is the right trade, but it erases
the difference between *"blocked/broken"* and *"nothing to report"* everywhere
downstream — the only trace is a log line nobody reads. This has already cost us
twice: Reddit 403ing on every cycle for weeks, and the benchmark tile silently
never rendering.

``FailOpenSource`` is a mixin that records the outcome of the last fetch so the
scan cycle can lift it into a ``HealthEvent`` and the dashboard can show it.
Adapters keep their existing behaviour exactly — they still return ``[]`` — they
just leave a note about why.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["FailOpenSource", "SourceOutcome"]


@dataclass(frozen=True)
class SourceOutcome:
    """What happened on the last fetch. ``error`` distinguishes the two cases
    that an empty result otherwise conflates."""

    name: str
    ok: bool
    item_count: int
    error: str | None = None

    @property
    def status(self) -> str:
        """``critical`` when the source actually failed; ``ok`` otherwise —
        including a successful fetch that returned nothing, which is a normal
        quiet-ticker outcome and must not read as a fault."""
        return "ok" if self.ok else "critical"


class FailOpenSource:
    """Mixin for adapters whose ``fetch()`` swallows everything.

    Call ``_record_ok(n)`` on the success path and ``_record_failure(exc)`` in
    the ``except``. ``last_outcome`` starts as ``None``, meaning "has not run
    this process" — deliberately distinct from a recorded success or failure so
    a never-invoked source doesn't masquerade as healthy.
    """

    #: Overridden per adapter so tiles read "reddit"/"apewisdom", not a class name.
    source_name: str = ""

    last_outcome: SourceOutcome | None = None

    def _name(self) -> str:
        return self.source_name or type(self).__name__

    def _record_ok(self, item_count: int) -> None:
        self.last_outcome = SourceOutcome(name=self._name(), ok=True, item_count=item_count)

    def _record_failure(self, exc: BaseException) -> None:
        self.last_outcome = SourceOutcome(
            name=self._name(), ok=False, item_count=0, error=f"{type(exc).__name__}: {exc}"[:200]
        )
