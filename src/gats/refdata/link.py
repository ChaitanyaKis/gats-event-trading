"""Tie each filing to a security (``announcement_security``).

Rules, in order:

- **NSE:** the ISIN, on the filing's date; failing that, the symbol as of
  ``first_seen_at``. NSE's API reports the symbol (and ISIN) current when it
  was *fetched*, so a backfilled 2024 Zomato filing says ETERNAL; resolving
  that symbol on the filing's own date would miss or mislink it.
- **BSE:** the scrip code, on the filing's date. Scrip codes are permanent.

Linking uses identifier windows on the event date, so a symbol that only
started after the filing can never claim it. It does use mappings first seen
later (the backward extension in ``gats.refdata.master``): that is identity,
not price information.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, and_, or_, select, true

from gats.db import repo
from gats.db.schema import announcement_security, announcements, securities
from gats.refdata.master import Resolver, latest_build_id
from gats.timeutil import to_ist

_BATCH = 2000


@dataclass
class LinkStats:
    build_id: int | None = None
    considered: int = 0
    linked: int = 0
    unresolved: int = 0
    by_method: dict[str, int] = field(default_factory=dict)


def link_one(row: Any, resolver: Resolver) -> tuple[int | None, str | None]:
    """``(security_id, method)`` for one announcement row."""
    event_day = to_ist(row.event_ts).date()
    if row.source == "NSE":
        if row.isin:
            sid = resolver.resolve("isin", row.isin, event_day)
            if sid is not None:
                return sid, "isin"
        if row.symbol:
            sid = resolver.resolve("nse_symbol", row.symbol, to_ist(row.first_seen_at).date())
            if sid is not None:
                return sid, "nse_symbol"
        return None, None
    if row.source == "BSE" and row.scrip_code:
        sid = resolver.resolve("bse_scrip", row.scrip_code, event_day)
        if sid is not None:
            return sid, "bse_scrip"
    return None, None


def _pending(
    conn: Connection, build_id: int, limit: int, ids: Collection[int] | None
) -> Sequence[Any]:
    """Unlinked filings, unresolved ones from older builds, and links to
    securities that a later build merged into another (among ``ids`` only,
    if given)."""
    merged = select(securities.c.security_id).where(securities.c.merged_into.is_not(None))
    link = announcement_security
    only = true() if ids is None else announcements.c.id.in_(list(ids))
    return conn.execute(
        select(
            announcements.c.id,
            announcements.c.source,
            announcements.c.symbol,
            announcements.c.isin,
            announcements.c.scrip_code,
            announcements.c.event_ts,
            announcements.c.first_seen_at,
        )
        .select_from(announcements.outerjoin(link, link.c.announcement_id == announcements.c.id))
        .where(
            or_(
                link.c.announcement_id.is_(None),
                and_(link.c.security_id.is_(None), link.c.build_id < build_id),
                link.c.security_id.in_(merged),
            ),
            only,
        )
        .order_by(announcements.c.id)
        .limit(limit)
    ).all()


def link_pending(
    conn: Connection,
    now: datetime,
    resolver: Resolver | None = None,
    *,
    max_rows: int = 10**7,
    ids: Collection[int] | None = None,
) -> LinkStats:
    """Link everything that needs it (or only ``ids``), against the latest
    master build."""
    stats = LinkStats(build_id=latest_build_id(conn))
    if stats.build_id is None:
        return stats  # no master yet: nothing to link against
    resolver = resolver or Resolver.load(conn, stats.build_id)
    while stats.considered < max_rows:
        rows = _pending(conn, stats.build_id, min(_BATCH, max_rows - stats.considered), ids)
        if not rows:
            break
        out = []
        for row in rows:
            security_id, method = link_one(row, resolver)
            out.append(
                {
                    "announcement_id": row.id,
                    "security_id": security_id,
                    "method": method,
                    "build_id": stats.build_id,
                    "linked_at": now,
                }
            )
            if security_id is None:
                stats.unresolved += 1
            else:
                stats.linked += 1
                stats.by_method[method or "?"] = stats.by_method.get(method or "?", 0) + 1
        repo.upsert(
            conn,
            announcement_security,
            out,
            ["announcement_id"],
            ["security_id", "method", "build_id", "linked_at"],
        )
        stats.considered += len(rows)
        if len(rows) < _BATCH:
            break
    return stats
