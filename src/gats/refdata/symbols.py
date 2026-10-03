"""NSE symbol history: which symbol a listing traded under on a given date.

NSE identifies a listing by its symbol, and symbols change (ZOMATO became
ETERNAL on 2025-04-09). Price files use the symbol of their own date, but
the announcements API reports the *current* symbol even for old filings
(verified 2026-10-02: a 2024-08-01 Zomato filing comes back as ETERNAL), so
every join between the two must translate symbols across dates.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import Connection, select

from gats.db import repo
from gats.db.schema import nse_symbol_changes
from gats.sources.nse_symbols import SymbolChangeRecord
from gats.timeutil import ensure_aware, ist_datetime


def store_changes(
    conn: Connection,
    records: Sequence[SymbolChangeRecord],
    *,
    fetched_at: datetime,
    raw_doc_id: str,
    parser_version: str,
) -> int:
    """Insert unseen changes; return how many were new.

    ``available_at`` is the earlier of the fetch time and 00:00 IST on the
    effective date: NSE announces symbol changes before they take effect, so
    a historical change was knowable by the morning it applied, while a
    change first seen ahead of its date is knowable from the fetch.
    """
    existing = {
        (r.old_symbol, r.new_symbol, r.effective_date)
        for r in conn.execute(
            select(
                nse_symbol_changes.c.old_symbol,
                nse_symbol_changes.c.new_symbol,
                nse_symbol_changes.c.effective_date,
            )
        )
    }
    rows = [
        {
            "old_symbol": r.old_symbol,
            "new_symbol": r.new_symbol,
            "effective_date": r.effective_date,
            "company_name": r.company_name,
            "available_at": min(ensure_aware(fetched_at), ist_datetime(r.effective_date, time())),
            "raw_doc_id": raw_doc_id,
            "parser_version": parser_version,
        }
        for r in records
        if (r.old_symbol, r.new_symbol, r.effective_date) not in existing
    ]
    repo.insert_ignore(
        conn, nse_symbol_changes, rows, ["old_symbol", "new_symbol", "effective_date"]
    )
    return len({(r["old_symbol"], r["new_symbol"], r["effective_date"]) for r in rows})


@dataclass(frozen=True, slots=True)
class Change:
    old: str
    new: str
    effective: date  # first session under ``new``


@dataclass(frozen=True, slots=True)
class SymbolWindow:
    symbol: str
    valid_from: date | None  # None: before any recorded change
    valid_to: date | None  # exclusive; None: still current


class SymbolHistory:
    """In-memory view of the change log (about a thousand rows).

    Built point-in-time: only changes with ``available_at <= as_of`` are
    visible, so a backtest cannot know about a rename before NSE did.
    Self-maps (old == new, seen on debt and ETF rows) carry no information
    and are dropped.
    """

    def __init__(self, changes: Sequence[Change]) -> None:
        self._changes = sorted((c for c in changes if c.old != c.new), key=lambda c: c.effective)
        self._by_new: dict[str, list[Change]] = {}
        self._by_old: dict[str, list[Change]] = {}
        for change in self._changes:
            self._by_new.setdefault(change.new, []).append(change)
            self._by_old.setdefault(change.old, []).append(change)

    @classmethod
    def load(cls, conn: Connection, as_of: datetime | None = None) -> SymbolHistory:
        query = select(
            nse_symbol_changes.c.old_symbol,
            nse_symbol_changes.c.new_symbol,
            nse_symbol_changes.c.effective_date,
        )
        if as_of is not None:
            query = query.where(nse_symbol_changes.c.available_at <= ensure_aware(as_of))
        return cls(
            [Change(r.old_symbol, r.new_symbol, r.effective_date) for r in conn.execute(query)]
        )

    def symbol_on(self, symbol: str, known_on: date, target: date) -> str:
        """The symbol, on ``target``, of the listing that was ``symbol`` on ``known_on``."""
        current, day = symbol.upper(), known_on
        if target < day:
            while True:
                # Latest rename *into* `current` that happened after `target`.
                candidates = [
                    c for c in self._by_new.get(current, []) if target < c.effective <= day
                ]
                if not candidates:
                    return current
                change = max(candidates, key=lambda c: c.effective)
                current, day = change.old, change.effective - timedelta(days=1)
        while True:
            # Earliest rename *out of* `current` that happened by `target`.
            candidates = [c for c in self._by_old.get(current, []) if day < c.effective <= target]
            if not candidates:
                return current
            change = min(candidates, key=lambda c: c.effective)
            current, day = change.new, change.effective

    def windows(self, symbol: str, known_on: date) -> list[SymbolWindow]:
        """Every symbol of the listing that was ``symbol`` on ``known_on``, oldest first.

        Walks back and forward *from* ``known_on``, so a symbol reused after
        an earlier holder renamed away is not glued onto that holder's chain:
        the reuse starts on the day the earlier holder left the symbol.
        """
        starts: list[tuple[str, date]] = []  # (symbol, first session), newest first
        current, day = symbol.upper(), known_on
        while True:
            into = [c for c in self._by_new.get(current, []) if c.effective <= day]
            if not into:
                break
            change = max(into, key=lambda c: c.effective)
            starts.append((current, change.effective))
            current, day = change.old, change.effective - timedelta(days=1)
        # The oldest symbol starts when a previous holder renamed away from
        # it, if one did; otherwise before any recorded change.
        left = [c.effective for c in self._by_old.get(current, []) if c.effective <= day]
        chain: list[tuple[str, date | None]] = [(current, max(left) if left else None)]
        chain += list(reversed(starts))

        current, day = symbol.upper(), known_on
        while True:
            out = [c for c in self._by_old.get(current, []) if c.effective > day]
            if not out:
                break
            change = min(out, key=lambda c: c.effective)
            chain.append((change.new, change.effective))
            current, day = change.new, change.effective

        return [
            SymbolWindow(sym, start, chain[i + 1][1] if i + 1 < len(chain) else None)
            for i, (sym, start) in enumerate(chain)
        ]
