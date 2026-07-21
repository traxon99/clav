"""clav-web — the Story 3.8 control API and Story 3.9 HTMX supervisory UI.

A **separate process** from ``clav-core`` (docs/03-database.md, "two
processes, one DB"): it reads the same SQLite (WAL) database and writes only
control/approval/config rows the core loop already polls. It never runs
trading logic itself.
"""

from __future__ import annotations
