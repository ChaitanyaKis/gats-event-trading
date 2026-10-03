"""How many announcements can be tied to a security (T2.1/T2.4 acceptance).

Unresolved filings are invisible to every study downstream, so coverage is
reported by distinct identifier *and* by filing, with the worst offenders
listed for inspection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import Connection, func, select

from gats.db.schema import announcement_security, announcements, bse_scrips


@dataclass
class Coverage:
    label: str
    n_filings: int = 0
    n_ids: int = 0
    resolved_filings: int = 0
    resolved_ids: int = 0
    unresolved: list[tuple[str, str | None, int]] = field(default_factory=list)

    @property
    def filing_share(self) -> float:
        return self.resolved_filings / self.n_filings if self.n_filings else 0.0

    @property
    def id_share(self) -> float:
        return self.resolved_ids / self.n_ids if self.n_ids else 0.0


def bse_scrip_isin_coverage(conn: Connection, since: datetime, top: int = 20) -> Coverage:
    """Share of BSE filings since ``since`` whose scrip code maps to an ISIN in
    the BSE scrip master (any version: a delisted scrip still resolves)."""
    counts = conn.execute(
        select(
            announcements.c.scrip_code,
            func.max(announcements.c.company_name),
            func.count(),
        )
        .where(
            announcements.c.source == "BSE",
            announcements.c.event_ts >= since,
            announcements.c.scrip_code.is_not(None),
        )
        .group_by(announcements.c.scrip_code)
    ).all()
    with_isin = set(
        conn.execute(
            select(bse_scrips.c.scrip_code).where(bse_scrips.c.isin.is_not(None)).distinct()
        ).scalars()
    )
    cov = Coverage(label="BSE scrip code -> ISIN")
    missing: list[tuple[str, str | None, int]] = []
    for code, name, n in counts:
        cov.n_ids += 1
        cov.n_filings += int(n)
        if code in with_isin:
            cov.resolved_ids += 1
            cov.resolved_filings += int(n)
        else:
            missing.append((str(code), name, int(n)))
    cov.unresolved = sorted(missing, key=lambda m: -m[2])[:top]
    return cov


def link_coverage(conn: Connection, source: str, since: datetime, top: int = 20) -> Coverage:
    """Share of ``source`` filings since ``since`` linked to a security (T2.4).

    Filings not yet considered by the linker count as unresolved: coverage
    must not look better because the linker is behind.
    """
    ident = announcements.c.symbol if source == "NSE" else announcements.c.scrip_code
    link = announcement_security
    rows = conn.execute(
        select(
            ident,
            func.max(announcements.c.company_name),
            func.count(),
            func.count(link.c.security_id),
        )
        .select_from(announcements.outerjoin(link, link.c.announcement_id == announcements.c.id))
        .where(announcements.c.source == source, announcements.c.event_ts >= since)
        .group_by(ident)
    ).all()
    cov = Coverage(label=f"{source} filings -> security")
    missing: list[tuple[str, str | None, int]] = []
    for value, name, n, n_linked in rows:
        cov.n_ids += 1
        cov.n_filings += int(n)
        cov.resolved_filings += int(n_linked)
        if n_linked == n:
            cov.resolved_ids += 1
        else:
            missing.append((str(value), name, int(n) - int(n_linked)))
    cov.unresolved = sorted(missing, key=lambda m: -m[2])[:top]
    return cov
