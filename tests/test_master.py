"""Security master (T2.3): validity windows, stable IDs, point-in-time lookups."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Connection, Engine, select

from gats.db import repo
from gats.db.schema import bse_scrips, securities, security_identifiers
from gats.rawstore import RawStore
from gats.refdata import master
from gats.refdata.ingest import BSE_SCRIP_ATTRS
from gats.refdata.master import OPEN_START, Resolver
from gats.refdata.symbols import store_changes
from gats.refdata.versions import apply_snapshot
from gats.sources.models import AnnouncementRecord, InstrumentRecord
from gats.sources.nse_symbols import SymbolChangeRecord

NOW = datetime(2026, 10, 2, 3, 0, tzinfo=UTC)
TODAY = date(2026, 10, 2)


class World:
    """Builds reference rows the way the real ingest paths write them."""

    def __init__(self, conn: Connection, store: RawStore) -> None:
        self.conn, self.store, self.n = conn, store, 0

    def doc(self) -> str:
        self.n += 1
        return repo.save_raw(
            self.conn,
            self.store,
            f"doc{self.n}".encode(),
            kind="t",
            source="X",
            url="u",
            content_type=None,
            fetched_at=NOW,
        )

    def nse_listed(self, day: date, *rows: tuple[str, str, str]) -> None:
        repo.upsert_instruments(
            self.conn,
            day,
            [InstrumentRecord(sym, "EQ", isin, name, None, 10.0, 1) for sym, isin, name in rows],
            raw_doc_id=self.doc(),
            parser_version="v",
            available_at=datetime.combine(day, datetime.min.time(), tzinfo=UTC)
            + timedelta(hours=3),
        )

    def bse_listed(self, day: date, *rows: tuple[str, str | None, str]) -> None:
        records: list[dict[str, Any]] = []
        for code, isin, name in rows:
            row: dict[str, Any] = {a: None for a in BSE_SCRIP_ATTRS}
            row.update(scrip_code=code, isin=isin, name=name, status="Active")
            records.append(row)
        apply_snapshot(
            self.conn,
            bse_scrips,
            kind="bse_scrips",
            key="scrip_code",
            attrs=BSE_SCRIP_ATTRS,
            as_of=day,
            records=records,
            available_at=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
            raw_doc_id=self.doc(),
            parser_version="v",
        )

    def renamed(self, old: str, new: str, effective: date) -> None:
        store_changes(
            self.conn,
            [SymbolChangeRecord(old, new, effective, None)],
            fetched_at=NOW,
            raw_doc_id=self.doc(),
            parser_version="v",
        )

    def nse_filing(self, ann_id: str, symbol: str, isin: str, seen: datetime) -> None:
        record = AnnouncementRecord(
            source="NSE",
            source_ann_id=ann_id,
            symbol=symbol,
            scrip_code=None,
            isin=isin,
            company_name=f"{symbol} Ltd",
            category=None,
            subcategory=None,
            subject=None,
            details=None,
            attachment_url=None,
            exch_submitted_ts=None,
            exch_disseminated_ts=seen,
            event_ts=seen,
        )
        repo.insert_announcements(
            self.conn,
            [record],
            raw_doc_id=self.doc(),
            parser_version="v",
            mode="backfill",
            fetched_at=seen,
            now=seen,
        )


@pytest.fixture
def world(engine: Engine, store: RawStore) -> Any:
    with engine.begin() as conn:
        yield World(conn, store)


def ids(conn: Connection, security_id: int) -> set[tuple[str, str, date, date | None]]:
    return {
        (r.id_type, r.value, r.valid_from, r.valid_to)
        for r in conn.execute(
            select(security_identifiers).where(
                security_identifiers.c.security_id == security_id,
                security_identifiers.c.last_build_id == master.latest_build_id(conn),
            )
        )
    }


def test_nse_chain_and_bse_scrip_join_on_isin(world: World) -> None:
    world.renamed("ZOMATO", "ETERNAL", date(2025, 4, 9))
    world.nse_listed(TODAY, ("ETERNAL", "INE758T01015", "Eternal Limited"))
    world.bse_listed(TODAY, ("543320", "INE758T01015", "ETERNAL LTD"))
    stats = master.build(world.conn, NOW)
    assert (stats.securities, stats.new_securities, stats.conflicts) == (1, 1, [])

    r = Resolver.load(world.conn)
    sid = r.resolve("bse_scrip", "543320", date(2024, 8, 1))
    assert sid is not None
    assert r.resolve("nse_symbol", "ZOMATO", date(2024, 8, 1)) == sid
    assert r.resolve("nse_symbol", "zomato", date(2025, 4, 8)) == sid  # case-insensitive
    assert r.resolve("nse_symbol", "ZOMATO", date(2025, 4, 9)) is None  # renamed that day
    assert r.resolve("nse_symbol", "ETERNAL", date(2025, 4, 9)) == sid
    assert r.resolve("nse_symbol", "ETERNAL", date(2024, 8, 1)) is None
    assert r.resolve("isin", "INE758T01015", date(2010, 1, 1)) == sid
    assert ids(world.conn, sid) == {
        ("nse_symbol", "ZOMATO", OPEN_START, date(2025, 4, 9)),
        ("nse_symbol", "ETERNAL", date(2025, 4, 9), None),
        ("bse_scrip", "543320", OPEN_START, None),
        ("isin", "INE758T01015", OPEN_START, None),
    }
    sec = world.conn.execute(select(securities).where(securities.c.security_id == sid)).one()
    assert (sec.primary_isin, sec.name) == ("INE758T01015", "Eternal Limited")  # NSE name wins


def test_backfilled_filing_is_anchored_on_its_fetch_date(world: World) -> None:
    # A delisted-from-snapshot company known only from a filing fetched in 2026
    # under its current symbol must still get its pre-rename symbol window.
    world.renamed("OLDCO", "NEWCO", date(2025, 1, 1))
    world.nse_filing("1", "NEWCO", "INE000X01011", datetime(2026, 10, 1, 5, 0, tzinfo=UTC))
    master.build(world.conn, NOW)
    r = Resolver.load(world.conn)
    sid = r.resolve("isin", "INE000X01011", date(2024, 6, 1))
    assert sid is not None and r.resolve("nse_symbol", "OLDCO", date(2024, 6, 1)) == sid


def test_ids_are_stable_across_rebuilds(world: World) -> None:
    world.nse_listed(TODAY, ("AAA", "INE00AAA1011", "A"), ("BBB", "INE00BBB1011", "B"))
    first = master.build(world.conn, NOW)
    before = {v: Resolver.load(world.conn).resolve("nse_symbol", v, TODAY) for v in ("AAA", "BBB")}
    # A new listing arriving between builds must not reshuffle existing IDs.
    world.nse_listed(TODAY, ("AAB", "INE00AAB1011", "AB"))
    second = master.build(world.conn, NOW + timedelta(days=1))
    after = {v: Resolver.load(world.conn).resolve("nse_symbol", v, TODAY) for v in ("AAA", "BBB")}
    assert before == after
    assert (first.new_securities, second.new_securities) == (2, 1)
    assert second.build_id == first.build_id + 1


def test_isin_change_under_one_scrip_merges_securities(world: World) -> None:
    # Before: BSE scrip 500100 has ISIN ...01011 and NSE lists XYZ with the
    # new ISIN ...01029 only: two securities. When BSE's list moves the scrip
    # to the new ISIN, the version history links both ISINs.
    world.bse_listed(date(2026, 9, 30), ("500100", "INE100X01011", "XYZ LTD"))
    world.nse_listed(date(2026, 9, 30), ("XYZ", "INE100X01029", "XYZ Limited"))
    master.build(world.conn, NOW)
    r = Resolver.load(world.conn)
    a, b = r.resolve("bse_scrip", "500100", TODAY), r.resolve("nse_symbol", "XYZ", TODAY)
    assert a is not None and b is not None and a != b

    world.bse_listed(date(2026, 10, 1), ("500100", "INE100X01029", "XYZ LTD"))
    stats = master.build(world.conn, NOW + timedelta(days=1))
    assert stats.merged == 1
    r = Resolver.load(world.conn)
    keep = min(a, b)
    assert r.resolve("bse_scrip", "500100", TODAY) == keep
    assert r.resolve("nse_symbol", "XYZ", TODAY) == keep
    assert r.resolve("isin", "INE100X01011", TODAY) == keep
    gone = world.conn.execute(
        select(securities.c.merged_into).where(securities.c.security_id == max(a, b))
    ).scalar_one()
    assert gone == keep


def test_known_at_hides_what_was_not_yet_knowable(world: World) -> None:
    world.renamed("OLDN", "NEWN", date(2025, 4, 9))  # knowable 2025-04-08 18:30 UTC
    world.nse_filing("1", "OLDN", "INE200X01011", datetime(2024, 1, 10, 5, 0, tzinfo=UTC))
    master.build(world.conn, NOW)
    r = Resolver.load(world.conn)
    # Everything known (research on history): the new symbol resolves.
    assert r.resolve("nse_symbol", "NEWN", date(2025, 5, 1)) is not None
    # A live system on 2025-04-01 could not yet know NEWN.
    assert (
        r.resolve("nse_symbol", "NEWN", date(2025, 5, 1), known_at=datetime(2025, 4, 1, tzinfo=UTC))
        is None
    )
    # ...but it knew OLDN from the filing it recorded in January 2024.
    assert (
        r.resolve("nse_symbol", "OLDN", date(2024, 3, 1), known_at=datetime(2024, 3, 1, tzinfo=UTC))
        is not None
    )


def test_one_symbol_two_isins_is_one_security(world: World) -> None:
    # No rename on record: the second ISIN is taken as an ISIN change (e.g. a
    # face-value split), the common case, not as a different company.
    world.nse_filing("1", "SPLITCO", "INE300X01011", datetime(2026, 1, 5, tzinfo=UTC))
    world.nse_listed(TODAY, ("SPLITCO", "INE300X01029", "Split Co"))
    stats = master.build(world.conn, NOW)
    assert stats.securities == 1
    r = Resolver.load(world.conn)
    assert r.resolve("isin", "INE300X01011", TODAY) == r.resolve("isin", "INE300X01029", TODAY)


def test_symbol_reused_after_rename_away_is_a_new_security(world: World) -> None:
    # OLDX renamed to NEWX on 2025-01-01; later a different company lists as OLDX.
    world.renamed("OLDX", "NEWX", date(2025, 1, 1))
    world.nse_listed(
        TODAY, ("NEWX", "INE400X01011", "First Co"), ("OLDX", "INE401X01019", "Second Co")
    )
    stats = master.build(world.conn, NOW)
    assert stats.securities == 2 and stats.conflicts == []
    r = Resolver.load(world.conn)
    first, second = (
        r.resolve("isin", "INE400X01011", TODAY),
        r.resolve("isin", "INE401X01019", TODAY),
    )
    assert first != second
    assert r.resolve("nse_symbol", "OLDX", date(2024, 6, 1)) == first
    assert r.resolve("nse_symbol", "OLDX", date(2025, 6, 1)) == second
    assert ids(world.conn, second) >= {("nse_symbol", "OLDX", date(2025, 1, 1), None)}


def test_bse_scrip_without_isin_is_its_own_security(world: World) -> None:
    world.bse_listed(TODAY, ("500011", None, "OLD DELISTED LTD"), ("500012", None, "OTHER"))
    stats = master.build(world.conn, NOW)
    assert stats.securities == 2


def test_unknown_id_type_is_rejected(world: World) -> None:
    with pytest.raises(ValueError):
        Resolver.load(world.conn).resolve("cusip", "x", TODAY)


def test_asof_resolves_only_up_to_its_clock(world: World) -> None:
    from gats.pit import AsOf

    world.renamed("OLDN", "NEWN", date(2025, 4, 9))
    world.nse_filing("1", "OLDN", "INE200X01011", datetime(2024, 1, 10, 5, 0, tzinfo=UTC))
    master.build(world.conn, NOW)
    clock = AsOf(world.conn, datetime(2025, 4, 1, 6, 0, tzinfo=UTC))
    sid = clock.security("nse_symbol", "OLDN")
    assert sid is not None
    with pytest.raises(ValueError, match="after the clock"):
        clock.security("nse_symbol", "NEWN", date(2025, 5, 1))
    clock.advance(datetime(2025, 5, 1, 6, 0, tzinfo=UTC))
    assert clock.security("nse_symbol", "NEWN") == sid
    # Strict mode: on 2024-01-01 the system had not yet seen the filing.
    early = AsOf(world.conn, datetime(2024, 1, 1, tzinfo=UTC))
    assert early.security("nse_symbol", "OLDN", strict=True) is None
    assert early.security("nse_symbol", "OLDN") == sid
