"""Corporate actions and split-safe daily returns.

NSE's bhavcopy ``PREV_CLOSE`` is the previous session's raw close, not
adjusted for splits or bonuses (verified on 10 real ex-dates, Sep 2025), so
``CLOSE / PREV_CLOSE - 1`` shows a fake -50%..-90% move on every ex-date. The
fix is the share multiplier: on an ex-date, one old share became ``m`` new
shares, so the holder's return is ``CLOSE * m / PREV_CLOSE - 1``. Several
actions on one day multiply (NAZARA, 2025-09-26: 1:1 bonus and a Rs 4 -> 2
split, m = 4).

Rights issues, demergers and bonus preference shares change value per share
in ways no multiplier captures; those days are reported by
``needs_review`` so studies exclude windows that contain them.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any

from sqlalchemy import Connection, select

from gats.db import repo
from gats.db.schema import corporate_actions
from gats.refdata.symbols import SymbolHistory
from gats.sources.nse_corp_actions import CorporateActionRecord
from gats.timeutil import ensure_aware, ist_datetime, to_ist


def store_actions(
    conn: Connection,
    records: Sequence[CorporateActionRecord],
    *,
    fetched_at: datetime,
    raw_doc_id: str,
    parser_version: str,
) -> int:
    """Insert unseen actions, refresh parsed fields of known ones; return new count."""
    fetched_at = ensure_aware(fetched_at)
    keys = {
        (r.reported_symbol, r.ex_date, r.subject)
        for r in conn.execute(
            select(
                corporate_actions.c.reported_symbol,
                corporate_actions.c.ex_date,
                corporate_actions.c.subject,
            )
        )
    }
    new_rows: list[dict[str, Any]] = []
    known_rows: list[dict[str, Any]] = []
    for r in records:
        row = {
            "reported_symbol": r.reported_symbol,
            "ex_date": r.ex_date,
            "subject": r.subject,
            "series": r.series,
            "isin": r.isin,
            "company": r.company,
            "record_date": r.record_date,
            "face_value": r.face_value,
            "kind": r.kind,
            "share_multiplier": r.share_multiplier,
            "cash_per_share": r.cash_per_share,
            "needs_review": r.needs_review,
            "available_at": min(fetched_at, ist_datetime(r.ex_date, time())),
            "first_seen_at": fetched_at,
            "raw_doc_id": raw_doc_id,
            "parser_version": parser_version,
        }
        (known_rows if (r.reported_symbol, r.ex_date, r.subject) in keys else new_rows).append(row)
    repo.insert_ignore(conn, corporate_actions, new_rows, ["reported_symbol", "ex_date", "subject"])
    # Re-seen rows: refresh what the parser derives, never the first-seen times.
    repo.upsert(
        conn,
        corporate_actions,
        known_rows,
        ["reported_symbol", "ex_date", "subject"],
        ["kind", "share_multiplier", "cash_per_share", "needs_review", "parser_version"],
    )
    return len({(r["reported_symbol"], r["ex_date"], r["subject"]) for r in new_rows})


@dataclass(frozen=True, slots=True)
class DayActions:
    multiplier: float
    needs_review: bool
    subjects: tuple[str, ...]


class ReturnAdjuster:
    """Share multipliers and review flags by (symbol on the ex-date, ex-date)."""

    def __init__(self, days: dict[tuple[str, date], DayActions]) -> None:
        self._days = days

    @classmethod
    def load(
        cls,
        conn: Connection,
        *,
        as_of: datetime | None = None,
        history: SymbolHistory | None = None,
    ) -> ReturnAdjuster:
        """``as_of`` hides actions not yet knowable then (point-in-time)."""
        history = history or SymbolHistory.load(conn)
        query = select(corporate_actions)
        if as_of is not None:
            query = query.where(corporate_actions.c.available_at <= ensure_aware(as_of))
        grouped: dict[tuple[str, date], list[tuple[float, bool, str]]] = defaultdict(list)
        for row in conn.execute(query):
            symbol = history.symbol_on(
                row.reported_symbol, to_ist(row.first_seen_at).date(), row.ex_date
            )
            grouped[(symbol, row.ex_date)].append(
                (row.share_multiplier or 1.0, bool(row.needs_review), row.subject)
            )
        days = {}
        for key, actions in grouped.items():
            multiplier = 1.0
            for m, _review, _subject in actions:
                multiplier *= m
            days[key] = DayActions(
                multiplier,
                any(review for _m, review, _s in actions),
                tuple(subject for _m, _r, subject in actions),
            )
        return cls(days)

    def on(self, symbol: str, day: date) -> DayActions | None:
        return self._days.get((symbol.upper(), day))

    def multiplier(self, symbol: str, day: date) -> float:
        found = self.on(symbol, day)
        return found.multiplier if found else 1.0

    def needs_review(self, symbol: str, day: date) -> bool:
        found = self.on(symbol, day)
        return bool(found and found.needs_review)

    def daily_return(
        self, symbol: str, day: date, prev_close: float | None, close: float | None
    ) -> float | None:
        """Split-safe close-to-close return for ``day`` (None if unknowable)."""
        if not prev_close or close is None or prev_close <= 0:
            return None
        return close * self.multiplier(symbol, day) / prev_close - 1.0
