"""Security master: one ``security_id`` per listed equity, across NSE and BSE.

How a build works (``gats refdata build``):

1. **Listings.** Each NSE rename chain (``SymbolHistory``) observed in the
   instrument snapshots or in NSE filings is one NSE listing; each BSE scrip
   code is one BSE listing. Every listing carries the ISINs seen with it.
2. **Securities.** Listings that share an ISIN are the same security
   (union-find over ISINs), so an NSE chain and a BSE scrip code meet, and an
   ISIN that changed under the same scrip code or symbol chain stays one
   security.
3. **Identifiers** get validity windows: NSE symbols from the rename chain,
   BSE scrip codes forever (BSE never reuses them), ISINs forever (never
   reused). Mappings first seen in today's snapshots are extended back to
   ``OPEN_START``: the sources publish no history, and research on old
   filings needs them. ``available_at`` keeps the honest "first knowable"
   time for anyone who must not use that extension.

IDs are stable across builds: a component reuses the ``security_id`` its
identifiers had in the previous build (the smallest one if two components
merged). Rows are upserted and stamped with the build, and lookups read only
the latest build, so a rebuild never deletes anything.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any

from sqlalchemy import Connection, func, select, update

from gats.db import repo
from gats.db.schema import (
    announcements,
    bse_scrips,
    instrument_snapshots,
    nse_symbol_changes,
    refdata_builds,
    securities,
    security_identifiers,
)
from gats.refdata.symbols import Change, SymbolHistory
from gats.timeutil import ensure_aware, ist_datetime, to_ist

OPEN_START = date(1900, 1, 1)
ID_TYPES = ("isin", "nse_symbol", "bse_scrip")
# NSE series that are equity listings (EQUITY_L.csv holds EQ, BE and BZ).
EQUITY_SERIES = frozenset({"EQ", "BE", "BZ", "SM", "ST"})

IdKey = tuple[str, str, date]  # (id_type, value, valid_from)


@dataclass
class _Window:
    id_type: str
    value: str
    valid_from: date
    valid_to: date | None
    source: str
    available_at: datetime
    # A window opened by a rename cannot be known before the rename was.
    floor: datetime | None = None

    def observe(self, at: datetime) -> None:
        candidate = max(at, self.floor) if self.floor else at
        self.available_at = min(self.available_at, candidate)


@dataclass
class _Listing:
    key: str  # "nse:LTI>LTIM>LTM" or "bse:500002"
    isins: dict[str, datetime] = field(default_factory=dict)  # isin -> first seen
    windows: list[_Window] = field(default_factory=list)
    # (observed_at, name, isin): the newest wins for display fields.
    names: list[tuple[datetime, str | None, str | None]] = field(default_factory=list)

    def see_isin(self, isin: str | None, at: datetime) -> None:
        if isin:
            self.isins[isin] = min(at, self.isins.get(isin, at))


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def _ist_date(ts: datetime) -> date:
    return to_ist(ts).date()


def _nse_listings(conn: Connection) -> dict[str, _Listing]:
    change_rows = conn.execute(
        select(
            nse_symbol_changes.c.old_symbol,
            nse_symbol_changes.c.new_symbol,
            nse_symbol_changes.c.effective_date,
            nse_symbol_changes.c.available_at,
        )
    ).all()
    history = SymbolHistory([Change(r[0], r[1], r[2]) for r in change_rows])
    change_known = {(r[1], r[2]): r[3] for r in change_rows}  # (new, effective) -> available

    # (symbol, known_on, isin, name, observed_at)
    observations: list[tuple[str, date, str | None, str | None, datetime]] = []
    for row in conn.execute(
        select(
            instrument_snapshots.c.symbol,
            instrument_snapshots.c.isin,
            instrument_snapshots.c.name,
            func.min(instrument_snapshots.c.as_of_date),
            func.min(instrument_snapshots.c.available_at),
            func.max(instrument_snapshots.c.available_at),
        )
        .where(instrument_snapshots.c.series.in_(EQUITY_SERIES))
        .group_by(
            instrument_snapshots.c.symbol, instrument_snapshots.c.isin, instrument_snapshots.c.name
        )
    ):
        observations.append((row[0], row[3], row[1], row[2], row[4]))
        observations.append((row[0], row[3], row[1], row[2], row[5]))  # newest name wins
    for ann in conn.execute(
        select(
            announcements.c.symbol,
            announcements.c.isin,
            func.max(announcements.c.company_name),
            func.min(announcements.c.first_seen_at),
        )
        .where(announcements.c.source == "NSE", announcements.c.symbol.is_not(None))
        .group_by(announcements.c.symbol, announcements.c.isin)
    ):
        # Filings report the symbol as of the fetch, so the chain is anchored
        # on first_seen_at, not on the filing's own date.
        observations.append((ann[0], _ist_date(ann[3]), ann[1], ann[2], ann[3]))

    listings: dict[str, _Listing] = {}
    for symbol, known_on, isin, name, observed_at in observations:
        chain = history.windows(symbol, known_on)
        key = "nse:" + ">".join(w.symbol for w in chain)
        listing = listings.get(key)
        if listing is None:
            listing = listings[key] = _Listing(key)
            for w in chain:
                started = change_known.get((w.symbol, w.valid_from)) if w.valid_from else None
                window = _Window(
                    "nse_symbol",
                    w.symbol,
                    w.valid_from or OPEN_START,
                    w.valid_to,
                    "nse_symbolchange" if started else "nse_observed",
                    max(observed_at, started) if started else observed_at,
                    floor=started,
                )
                listing.windows.append(window)
        for window in listing.windows:  # the earliest evidence wins
            window.observe(observed_at)
        listing.see_isin(isin, observed_at)
        listing.names.append((observed_at, name, isin))
    return listings


def _bse_listings(conn: Connection) -> dict[str, _Listing]:
    listings: dict[str, _Listing] = {}
    for row in conn.execute(select(bse_scrips).order_by(bse_scrips.c.valid_from)):
        key = f"bse:{row.scrip_code}"
        listing = listings.get(key)
        if listing is None:
            listing = listings[key] = _Listing(key)
            listing.windows.append(
                _Window(
                    "bse_scrip", row.scrip_code, OPEN_START, None, "bse_scrips", row.available_at
                )
            )
        listing.see_isin(row.isin, row.available_at)
        listing.names.append((row.available_at, row.issuer_name or row.name, row.isin))
    return listings


@dataclass
class BuildStats:
    build_id: int = 0
    listings: int = 0
    securities: int = 0
    new_securities: int = 0
    merged: int = 0
    identifiers: int = 0
    conflicts: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "listings": self.listings,
            "securities": self.securities,
            "new_securities": self.new_securities,
            "merged": self.merged,
            "identifiers": self.identifiers,
            "conflicts": len(self.conflicts),
        }


def _insert_id(conn: Connection, stmt: Any) -> int:
    key = conn.execute(stmt).inserted_primary_key
    if key is None or key[0] is None:
        raise RuntimeError("database did not return the new row's id")
    return int(key[0])


def latest_build_id(conn: Connection) -> int | None:
    value: int | None = conn.execute(select(func.max(refdata_builds.c.build_id))).scalar()
    return value


def build(conn: Connection, now: datetime) -> BuildStats:
    """Rebuild the security master from the reference tables (idempotent)."""
    stats = BuildStats()
    listings = {**_nse_listings(conn), **_bse_listings(conn)}
    stats.listings = len(listings)

    uf = _UnionFind()
    for listing in listings.values():
        uf.find(listing.key)
        for isin in listing.isins:
            uf.union(listing.key, f"isin:{isin}")
    components: dict[str, list[_Listing]] = defaultdict(list)
    for listing in listings.values():
        components[uf.find(listing.key)].append(listing)

    previous_build = latest_build_id(conn)
    previous: dict[IdKey, int] = {}
    if previous_build is not None:
        for row in conn.execute(
            select(
                security_identifiers.c.id_type,
                security_identifiers.c.value,
                security_identifiers.c.valid_from,
                security_identifiers.c.security_id,
            ).where(security_identifiers.c.last_build_id == previous_build)
        ):
            previous[(row[0], row[1], row[2])] = row[3]

    build_id = _insert_id(
        conn, refdata_builds.insert().values(built_at=ensure_aware(now), stats={})
    )
    stats.build_id = build_id

    claimed: set[int] = set()
    windows_by_key: dict[IdKey, tuple[int, _Window]] = {}
    # Deterministic order, so ID reuse does not depend on dict ordering.
    for root in sorted(components):
        members = components[root]
        windows = _component_windows(members)
        old_ids = sorted({previous[k] for k in windows if k in previous} - claimed)
        if old_ids:
            security_id = old_ids[0]
            for merged in old_ids[1:]:
                conn.execute(
                    update(securities)
                    .where(securities.c.security_id == merged)
                    .values(merged_into=security_id, last_build_id=build_id)
                )
                claimed.add(merged)
                stats.merged += 1
        else:
            security_id = _insert_id(
                conn, securities.insert().values(first_build_id=build_id, last_build_id=build_id)
            )
            stats.new_securities += 1
        claimed.add(security_id)
        primary_isin, display_name = _display(members)
        conn.execute(
            update(securities)
            .where(securities.c.security_id == security_id)
            .values(
                primary_isin=primary_isin,
                name=display_name,
                merged_into=None,
                last_build_id=build_id,
            )
        )
        for key, window in windows.items():
            if key in windows_by_key and windows_by_key[key][0] != security_id:
                # Same identifier, same start, two securities: a reused symbol
                # with no recorded change. Keep the more recent evidence.
                other_id, other = windows_by_key[key]
                stats.conflicts.append(
                    f"{key[0]} {key[1]}: securities {other_id} and {security_id}"
                )
                if other.available_at >= window.available_at:
                    continue
            windows_by_key[key] = (security_id, window)
        stats.securities += 1

    rows = [
        {
            "id_type": w.id_type,
            "value": w.value,
            "valid_from": w.valid_from,
            "security_id": sid,
            "valid_to": w.valid_to,
            "source": w.source,
            "available_at": w.available_at,
            "last_build_id": build_id,
        }
        for sid, w in windows_by_key.values()
    ]
    repo.upsert(
        conn,
        security_identifiers,
        rows,
        ["id_type", "value", "valid_from"],
        ["security_id", "valid_to", "source", "available_at", "last_build_id"],
    )
    stats.identifiers = len(rows)
    conn.execute(
        update(refdata_builds)
        .where(refdata_builds.c.build_id == build_id)
        .values(stats=stats.as_dict())
    )
    return stats


def _component_windows(members: Iterable[_Listing]) -> dict[IdKey, _Window]:
    windows: dict[IdKey, _Window] = {}
    for listing in members:
        candidates = list(listing.windows)
        candidates += [
            _Window("isin", isin, OPEN_START, None, listing.key.split(":")[0], seen)
            for isin, seen in listing.isins.items()
        ]
        for window in candidates:
            key = (window.id_type, window.value, window.valid_from)
            kept = windows.get(key)
            if kept is None or window.available_at < kept.available_at:
                windows[key] = window
    return windows


def _display(members: Iterable[_Listing]) -> tuple[str | None, str | None]:
    """Newest ISIN and name; NSE names win ties (they are the cleaner ones)."""
    seen = [
        (at, listing.key.startswith("nse:"), name, isin)
        for listing in members
        for at, name, isin in listing.names
    ]
    if not seen:
        return None, None
    seen.sort(key=lambda s: (s[0], s[1]))
    isin = next((s[3] for s in reversed(seen) if s[3]), None)
    name = next((s[2] for s in reversed(seen) if s[2]), None)
    return isin, name


class Resolver:
    """In-memory identifier lookup for one build (bulk linking needs it).

    ``known_at`` restricts the view to identifiers the system could have tied
    to a security by then; leave it None to use everything (including the
    backward extension of today's mappings).
    """

    def __init__(self, rows: Iterable[Any]) -> None:
        """``rows``: (id_type, value, valid_from, valid_to, security_id, available_at)."""
        self._index: dict[tuple[str, str], list[tuple[date, date | None, int, datetime]]] = (
            defaultdict(list)
        )
        for id_type, value, start, end, security_id, available_at in rows:
            self._index[(id_type, value.upper())].append((start, end, security_id, available_at))

    @classmethod
    def load(cls, conn: Connection, build_id: int | None = None) -> Resolver:
        build_id = build_id if build_id is not None else latest_build_id(conn)
        if build_id is None:
            return cls([])
        return cls(
            conn.execute(
                select(
                    security_identifiers.c.id_type,
                    security_identifiers.c.value,
                    security_identifiers.c.valid_from,
                    security_identifiers.c.valid_to,
                    security_identifiers.c.security_id,
                    security_identifiers.c.available_at,
                ).where(security_identifiers.c.last_build_id == build_id)
            )
        )

    def resolve(
        self, id_type: str, value: str, at: date, known_at: datetime | None = None
    ) -> int | None:
        """The security that ``value`` identified on ``at``; None if unknown or ambiguous."""
        if id_type not in ID_TYPES:
            raise ValueError(f"unknown identifier type {id_type!r}")
        found = {
            security_id
            for start, end, security_id, available_at in self._index.get(
                (id_type, value.upper()), []
            )
            if start <= at
            and (end is None or at < end)
            and (known_at is None or available_at <= known_at)
        }
        return found.pop() if len(found) == 1 else None


def resolve(
    conn: Connection, id_type: str, value: str, at: date, known_at: datetime | None = None
) -> int | None:
    """One-off lookup; use :class:`Resolver` for many."""
    return Resolver.load(conn).resolve(id_type, value, at, known_at)


def at_start_of(day: date) -> datetime:
    """00:00 IST on ``day``, as UTC (a convenient ``known_at``)."""
    return ist_datetime(day, time())
