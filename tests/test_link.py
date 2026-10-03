"""Linking filings to securities (T2.4)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, select

from gats.db.schema import announcement_security
from gats.ingest import Services
from gats.rawstore import RawStore
from gats.recorder import LinkJob, MasterBuildJob
from gats.refdata import master
from gats.refdata.coverage import link_coverage
from gats.refdata.link import link_pending
from gats.sources.models import AnnouncementRecord
from tests.conftest import FakeClock
from tests.test_master import NOW, TODAY, World


def links(world: World) -> dict[str, tuple[int | None, str | None, int]]:
    from gats.db.schema import announcements

    rows = world.conn.execute(
        select(
            announcements.c.source_ann_id,
            announcement_security.c.security_id,
            announcement_security.c.method,
            announcement_security.c.build_id,
        ).select_from(
            announcements.join(
                announcement_security,
                announcement_security.c.announcement_id == announcements.c.id,
            )
        )
    ).all()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


def bse_filing(world: World, ann_id: str, code: str, at: datetime) -> None:
    from gats.db import repo

    record = AnnouncementRecord(
        source="BSE",
        source_ann_id=ann_id,
        symbol=None,
        scrip_code=code,
        isin=None,
        company_name=f"Co {code}",
        category=None,
        subcategory=None,
        subject=None,
        details=None,
        attachment_url=None,
        exch_submitted_ts=None,
        exch_disseminated_ts=at,
        event_ts=at,
    )
    repo.insert_announcements(
        world.conn,
        [record],
        raw_doc_id=world.doc(),
        parser_version="v",
        mode="backfill",
        fetched_at=at,
        now=at,
    )


@pytest.fixture
def world(engine: Engine, store: RawStore) -> Any:
    with engine.begin() as conn:
        yield World(conn, store)


def test_rules_per_source(world: World) -> None:
    world.renamed("ZOMATO", "ETERNAL", date(2025, 4, 9))
    world.nse_listed(TODAY, ("ETERNAL", "INE758T01015", "Eternal Limited"))
    world.bse_listed(TODAY, ("543320", "INE758T01015", "ETERNAL LTD"))
    seen = datetime(2026, 10, 1, 5, 0, tzinfo=UTC)
    # Backfilled 2024 filing: NSE reports today's symbol and ISIN.
    world.nse_filing("isin", "ETERNAL", "INE758T01015", seen)
    bse_filing(world, "bse", "543320", datetime(2024, 8, 1, 9, 0, tzinfo=UTC))
    bse_filing(world, "reit", "542602", datetime(2024, 8, 1, 9, 0, tzinfo=UTC))
    master.build(world.conn, NOW)
    # Arrives after the build with an ISIN the master has never seen: the
    # symbol, as of the fetch, still links it.
    world.nse_filing("sym", "ETERNAL", "INE999Z99999", seen)

    stats = link_pending(world.conn, NOW)
    assert (stats.linked, stats.unresolved) == (3, 1)
    got = links(world)
    sid = got["isin"][0]
    assert sid is not None
    assert got["isin"][:2] == (sid, "isin")
    assert got["sym"][:2] == (sid, "nse_symbol")
    assert got["bse"][:2] == (sid, "bse_scrip")
    assert got["reit"][:2] == (None, None)


def test_symbol_fallback_uses_the_fetch_date_not_the_event_date(world: World) -> None:
    # Fetched on 2026-10-01 as ETERNAL; on the event date the symbol was
    # ZOMATO, so resolving ETERNAL on the event date would find nothing.
    world.renamed("ZOMATO", "ETERNAL", date(2025, 4, 9))
    world.nse_listed(TODAY, ("ETERNAL", "INE758T01015", "Eternal Limited"))
    master.build(world.conn, NOW)
    world.nse_filing("old", "ETERNAL", "INE000BAD000", datetime(2026, 10, 1, tzinfo=UTC))
    link_pending(world.conn, NOW)
    assert links(world)["old"][1] == "nse_symbol"


def test_idempotent_and_unresolved_retried_only_after_a_new_build(world: World) -> None:
    bse_filing(world, "late", "500777", datetime(2026, 9, 30, 6, 0, tzinfo=UTC))
    master.build(world.conn, NOW)
    first = link_pending(world.conn, NOW)
    assert (first.considered, first.unresolved) == (1, 1)
    assert link_pending(world.conn, NOW).considered == 0  # nothing to redo

    world.bse_listed(TODAY, ("500777", "INE777A01011", "LATE LISTED LTD"))
    assert link_pending(world.conn, NOW).considered == 0  # same build: still nothing
    master.build(world.conn, NOW + timedelta(days=1))
    again = link_pending(world.conn, NOW + timedelta(days=1))
    assert (again.considered, again.linked) == (1, 1)
    assert links(world)["late"][2] == master.latest_build_id(world.conn)


def test_links_follow_a_merge(world: World) -> None:
    world.bse_listed(date(2026, 9, 30), ("500100", "INE100X01011", "XYZ LTD"))
    world.nse_listed(date(2026, 9, 30), ("XYZ", "INE100X01029", "XYZ Limited"))
    world.nse_filing("n", "XYZ", "INE100X01029", datetime(2026, 9, 30, 6, tzinfo=UTC))
    bse_filing(world, "b", "500100", datetime(2026, 9, 30, 6, tzinfo=UTC))
    master.build(world.conn, NOW)
    link_pending(world.conn, NOW)
    before = links(world)
    assert before["n"][0] != before["b"][0]

    world.bse_listed(date(2026, 10, 1), ("500100", "INE100X01029", "XYZ LTD"))
    master.build(world.conn, NOW + timedelta(days=1))
    redo = link_pending(world.conn, NOW + timedelta(days=1))
    assert redo.considered == 1  # only the link pointing at the merged-away id
    after = links(world)
    assert after["n"][0] == after["b"][0] == min(before["n"][0] or 0, before["b"][0] or 0)


def test_nothing_happens_without_a_master(world: World) -> None:
    bse_filing(world, "x", "500001", datetime(2026, 9, 30, tzinfo=UTC))
    assert link_pending(world.conn, NOW).build_id is None
    assert links(world) == {}


def test_coverage_counts_unlinked_filings_as_unresolved(world: World) -> None:
    world.bse_listed(TODAY, ("500001", "INE001A01011", "A"))
    for i, code in enumerate(["500001", "500001", "500002"]):
        bse_filing(world, f"f{i}", code, datetime(2026, 9, 30, tzinfo=UTC))
    master.build(world.conn, NOW)
    since = datetime(2026, 9, 1, tzinfo=UTC)
    assert link_coverage(world.conn, "BSE", since).resolved_filings == 0  # linker not run yet
    link_pending(world.conn, NOW)
    cov = link_coverage(world.conn, "BSE", since)
    assert (cov.resolved_filings, cov.n_filings, cov.resolved_ids, cov.n_ids) == (2, 3, 1, 2)
    assert cov.unresolved == [("500002", "Co 500002", 1)]


class TestJobs:
    async def test_master_builds_once_a_day_after_its_window(
        self, svc: Services, clock: FakeClock
    ) -> None:
        job = MasterBuildJob("master_build", 1800, svc.settings.master_build_after_ist)
        clock.now = datetime(2026, 10, 2, 3, 0, tzinfo=UTC)  # 08:30 IST
        assert (await job.run_once(svc)).meta == {"skipped": "before daily window"}
        clock.now = datetime(2026, 10, 2, 4, 0, tzinfo=UTC)  # 09:30 IST
        assert (await job.run_once(svc)).ok
        assert (await job.run_once(svc)).meta == {"skipped": "already built today"}
        clock.now += timedelta(days=1)
        assert "securities" in (await job.run_once(svc)).meta

    async def test_link_job_reloads_resolver_on_new_build(self, svc: Services) -> None:
        job = LinkJob("link", 60)
        assert (await job.run_once(svc)).meta == {"skipped": "no security master yet"}
        with svc.engine.begin() as conn:
            master.build(conn, NOW)
        assert (await job.run_once(svc)).ok
        first = job._resolver
        assert (await job.run_once(svc)).ok and job._resolver is first  # cached
        with svc.engine.begin() as conn:
            master.build(conn, NOW + timedelta(days=1))
        await job.run_once(svc)
        assert job._resolver is not first
