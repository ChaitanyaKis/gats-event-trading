"""Behaviour added after the first live run (2026-09-28): BSE throttling,
backfill completeness, gap reconciliation, catch-up depth, schema upgrade."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import select, text

from gats.db import repo
from gats.db.engine import SchemaVersionError, init_db, make_engine
from gats.db.schema import SCHEMA_VERSION, announcements, backfill_days, metadata
from gats.ingest import Services, backfill_day, collect_bse
from gats.recorder import BseAnnouncementsJob, ReconcileJob
from tests.conftest import (
    BSE_URL,
    NSE_URL,
    FakeClock,
    SleepRecorder,
    bse_payload,
    bse_row,
    nse_row,
)

DAY = date(2026, 9, 25)
HTML_BLOCK = b"<!DOCTYPE html><html><body>Access Denied</body></html>"


def page(n: int, total: int, ids: list[str]) -> httpx.Response:
    rows = [bse_row(i, f"2026-09-25T10:{59 - k:02d}:00") for k, i in enumerate(ids)]
    return httpx.Response(200, content=bse_payload(rows, total_pages=total))


def status_of(svc: Services, source: str, day: date) -> tuple[str, int] | None:
    with svc.engine.begin() as conn:
        row = conn.execute(
            select(backfill_days.c.status, backfill_days.c.attempts).where(
                backfill_days.c.source == source, backfill_days.c.day == day
            )
        ).first()
    return None if row is None else (row.status, row.attempts)


class TestBseThrottling:
    @respx.mock
    async def test_transient_empty_page_is_retried(
        self, svc: Services, sleeper: SleepRecorder
    ) -> None:
        respx.get(BSE_URL, params__contains={"pageno": "1"}).mock(
            return_value=page(1, 2, ["a", "b"])
        )
        p2 = respx.get(BSE_URL, params__contains={"pageno": "2"}).mock(
            side_effect=[httpx.Response(200, content=b"{}"), page(2, 2, ["c"])]
        )
        outcome = await collect_bse(
            svc, DAY, DAY, job="t", mode="backfill", max_pages=50, stop_when_no_new=False
        )
        assert outcome.ok and outcome.meta["complete"]
        assert outcome.n_new == 3 and p2.call_count == 2
        assert sleeper.calls == [5.0]

    @respx.mock
    async def test_persistent_block_is_incomplete(
        self, svc: Services, sleeper: SleepRecorder
    ) -> None:
        respx.get(BSE_URL, params__contains={"pageno": "1"}).mock(
            return_value=page(1, 3, ["a", "b"])
        )
        respx.get(BSE_URL, params__contains={"pageno": "2"}).mock(
            return_value=httpx.Response(200, content=HTML_BLOCK)
        )
        p3 = respx.get(BSE_URL, params__contains={"pageno": "3"}).mock(
            return_value=page(3, 3, ["z"])
        )
        outcome = await collect_bse(
            svc, DAY, DAY, job="t", mode="backfill", max_pages=50, stop_when_no_new=False
        )
        assert not outcome.ok and not outcome.meta["complete"]
        assert "Access Denied" in (outcome.error or "")  # payload preview in the error
        assert sleeper.calls == [5.0, 10.0]  # linear backoff between retries
        assert p3.call_count == 0

    @respx.mock
    async def test_live_page_one_is_not_retried(
        self, svc: Services, sleeper: SleepRecorder
    ) -> None:
        route = respx.get(BSE_URL).mock(return_value=httpx.Response(200, content=b"{}"))
        outcome = await collect_bse(
            svc, DAY, DAY, job="t", mode="live", max_pages=10, stop_when_no_new=True
        )
        assert outcome.ok and route.call_count == 1 and sleeper.calls == []

    @respx.mock
    async def test_page_cap_means_incomplete(self, svc: Services) -> None:
        respx.get(BSE_URL, params__contains={"pageno": "1"}).mock(return_value=page(1, 5, ["a"]))
        respx.get(BSE_URL, params__contains={"pageno": "2"}).mock(return_value=page(2, 5, ["b"]))
        outcome = await collect_bse(
            svc, DAY, DAY, job="t", mode="backfill", max_pages=2, stop_when_no_new=False
        )
        assert outcome.ok and not outcome.meta["complete"]

    @respx.mock
    async def test_warmup_loads_bse_homepage_once(self, svc: Services) -> None:
        svc.settings.bse_warmup = True
        home = respx.get(svc.settings.bse_referer).mock(return_value=httpx.Response(200))
        respx.get(BSE_URL).mock(return_value=page(1, 1, ["a"]))
        for _ in range(2):
            await collect_bse(
                svc, DAY, DAY, job="t", mode="live", max_pages=1, stop_when_no_new=True
            )
        assert home.call_count == 1


class TestBackfillDay:
    @respx.mock
    async def test_partial_day_is_retried_until_complete(self, svc: Services) -> None:
        respx.get(BSE_URL, params__contains={"pageno": "1"}).mock(
            return_value=page(1, 2, ["a", "b"])
        )
        p2 = respx.get(BSE_URL, params__contains={"pageno": "2"}).mock(
            return_value=httpx.Response(200, content=b"{}")
        )
        first = await backfill_day(svc, "BSE", DAY, job="t", max_pages=50)
        assert first.meta["backfill_status"] == "incomplete"
        assert status_of(svc, "BSE", DAY) == ("incomplete", 1)

        p2.mock(return_value=page(2, 2, ["c"]))
        second = await backfill_day(svc, "BSE", DAY, job="t", max_pages=50)
        assert second.meta["backfill_status"] == "complete"
        assert second.n_new == 1  # a and b were kept from the partial run
        assert status_of(svc, "BSE", DAY) == ("complete", 2)

    @respx.mock
    async def test_gives_up_after_max_attempts(self, svc: Services) -> None:
        svc.settings.reconcile_max_attempts = 2
        respx.get(BSE_URL).mock(return_value=httpx.Response(200, content=b"{}"))
        await backfill_day(svc, "BSE", DAY, job="t", max_pages=5)
        await backfill_day(svc, "BSE", DAY, job="t", max_pages=5)
        assert status_of(svc, "BSE", DAY) == ("gave_up", 2)

    @respx.mock
    async def test_network_failure_does_not_count_as_attempt(self, svc: Services) -> None:
        respx.get(BSE_URL).mock(side_effect=httpx.ConnectError("offline"))
        outcome = await backfill_day(svc, "BSE", DAY, job="t", max_pages=5)
        assert outcome.meta["backfill_status"] == "incomplete"
        assert status_of(svc, "BSE", DAY) is None

    @respx.mock
    async def test_nse_day(self, svc: Services) -> None:
        import json

        respx.get(svc.settings.nse_home_url).mock(return_value=httpx.Response(200))
        respx.get(NSE_URL).mock(
            return_value=httpx.Response(200, content=json.dumps([nse_row("n1")]).encode())
        )
        outcome = await backfill_day(svc, "NSE", DAY, job="t", max_pages=1)
        assert outcome.meta["backfill_status"] == "complete"


class TestReconcile:
    @respx.mock
    async def test_fills_only_days_that_are_not_complete(
        self, svc: Services, clock: FakeClock
    ) -> None:
        clock.now = datetime(2026, 9, 28, 6, 0, tzinfo=UTC)  # Mon 11:30 IST
        with svc.engine.begin() as conn:
            repo.record_backfill_day(
                conn,
                "BSE",
                date(2026, 9, 26),
                complete=True,
                n_records=5,
                error=None,
                now=clock.now,
                max_attempts=3,
            )
        route = respx.get(BSE_URL).mock(return_value=page(1, 1, ["x"]))
        job = ReconcileJob("reconcile", 3600, days=3, sources=("BSE",), max_pages=10)

        assert job.due(svc) == [("BSE", date(2026, 9, 25)), ("BSE", date(2026, 9, 27))]
        outcome = await job.run_once(svc)
        assert outcome.ok
        asked = sorted(c.request.url.params["strPrevDate"] for c in route.calls)
        assert asked == ["20260925", "20260927"]
        assert job.due(svc) == []

    @respx.mock
    async def test_failures_do_not_fail_the_job(self, svc: Services) -> None:
        respx.get(BSE_URL).mock(return_value=httpx.Response(200, content=HTML_BLOCK))
        job = ReconcileJob("reconcile", 3600, days=1, sources=("BSE",), max_pages=10)
        outcome = await job.run_once(svc)
        assert outcome.ok and outcome.warnings


class TestCatchupDepth:
    @respx.mock
    async def test_first_run_pages_deeper_than_live(self, svc: Services) -> None:
        routes = [
            respx.get(BSE_URL, params__contains={"pageno": str(n)}).mock(
                return_value=page(n, 15, [f"p{n}"])
            )
            for n in range(1, 16)
        ]
        job = BseAnnouncementsJob(
            "bse", 30, 2.0, time(0), time(7), max_pages=10, catchup_max_pages=100
        )
        await job.run_once(svc)
        assert all(r.call_count == 1 for r in routes)  # all 15 pages, not capped at 10
        with svc.engine.begin() as conn:
            assert len(conn.execute(select(announcements.c.id)).all()) == 15


class TestSchemaUpgrade:
    def test_v1_database_is_upgraded_in_place(self, tmp_path: Path) -> None:
        engine = make_engine(f"sqlite:///{tmp_path / 'old.db'}")
        v1_tables = [t for name, t in metadata.tables.items() if name != "backfill_days"]
        metadata.create_all(engine, tables=v1_tables)
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO schema_meta VALUES ('schema_version', '1')"))
        init_db(engine)
        with engine.begin() as conn:
            version = conn.execute(
                text("SELECT value FROM schema_meta WHERE key='schema_version'")
            ).scalar_one()
            conn.execute(select(backfill_days)).all()  # table exists
        assert int(version) == SCHEMA_VERSION == 2
        engine.dispose()

    def test_newer_database_is_refused(self, tmp_path: Path) -> None:
        engine = make_engine(f"sqlite:///{tmp_path / 'new.db'}")
        init_db(engine)
        with engine.begin() as conn:
            conn.execute(text("UPDATE schema_meta SET value='99'"))
        with pytest.raises(SchemaVersionError):
            init_db(engine)
        engine.dispose()
