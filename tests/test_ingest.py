from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta

import httpx
import respx
from sqlalchemy import func, select

from gats import ingest
from gats.db import repo
from gats.db.schema import (
    announcements,
    eod_prices,
    fetch_log,
    instrument_snapshots,
    price_bands,
    raw_documents,
)
from gats.ingest import Services
from gats.recorder import BseAnnouncementsJob, EodJob, SnapshotJob
from tests.conftest import (
    BANDS_CSV,
    BSE_URL,
    EOD_CSV,
    INSTRUMENTS_CSV,
    NSE_HOME,
    NSE_URL,
    FakeClock,
    bse_payload,
    bse_row,
    nse_row,
)

LIVE = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"
HIST = "https://www.bseindia.com/xml-data/corpfiling/AttachHis/"
EOD_URL = "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{}.csv"


def count(svc: Services, table: object, *where: object) -> int:
    with svc.engine.begin() as conn:
        query = select(func.count()).select_from(table)  # type: ignore[arg-type]
        for clause in where:
            query = query.where(clause)  # type: ignore[arg-type]
        return int(conn.execute(query).scalar_one())


def bse_page(route_params: dict[str, str]) -> respx.Route:
    return respx.get(BSE_URL, params__contains=route_params)


def stamp(minute: int) -> str:
    return f"2026-09-25T10:{minute:02d}:00"


class TestCollectBse:
    @respx.mock
    async def test_live_paging_stops_when_nothing_new(self, svc: Services) -> None:
        p1 = bse_page({"pageno": "1"}).mock(
            return_value=httpx.Response(
                200,
                content=bse_payload(
                    [bse_row("n4", stamp(4)), bse_row("n3", stamp(3))], total_pages=2
                ),
            )
        )
        p2 = bse_page({"pageno": "2"}).mock(
            return_value=httpx.Response(
                200,
                content=bse_payload(
                    [bse_row("n2", stamp(2)), bse_row("n1", stamp(1))], total_pages=2
                ),
            )
        )

        first = await ingest.collect_bse(
            svc,
            date(2026, 9, 25),
            date(2026, 9, 25),
            job="t",
            mode="live",
            max_pages=10,
            stop_when_no_new=True,
        )
        assert (first.ok, first.n_new, p1.call_count, p2.call_count) == (True, 4, 1, 1)

        second = await ingest.collect_bse(
            svc,
            date(2026, 9, 25),
            date(2026, 9, 25),
            job="t",
            mode="live",
            max_pages=10,
            stop_when_no_new=True,
        )
        # Newest-first and page 1 fully known: page 2 is not requested again.
        assert (second.n_new, p1.call_count, p2.call_count) == (0, 2, 1)
        # Raw payload kept once per page that brought new rows; not for the idle poll.
        assert count(svc, raw_documents) == 2
        assert count(svc, fetch_log) == 3

    @respx.mock
    async def test_oldest_first_api_is_paged_from_the_end(self, svc: Services) -> None:
        pages = {
            "1": [bse_row("o1", stamp(1)), bse_row("o2", stamp(2))],
            "2": [bse_row("o3", stamp(3)), bse_row("o4", stamp(4))],
            "3": [bse_row("o5", stamp(5)), bse_row("o6", stamp(6))],
        }
        routes = {
            n: bse_page({"pageno": n}).mock(
                side_effect=lambda _req, n=n: httpx.Response(
                    200, content=bse_payload([dict(r) for r in pages[n]], total_pages=3)
                )
            )
            for n in pages
        }
        await ingest.collect_bse(
            svc,
            date(2026, 9, 25),
            date(2026, 9, 25),
            job="t",
            mode="live",
            max_pages=10,
            stop_when_no_new=True,
        )
        assert count(svc, announcements) == 6

        pages["3"].append(bse_row("o7", stamp(7)))
        outcome = await ingest.collect_bse(
            svc,
            date(2026, 9, 25),
            date(2026, 9, 25),
            job="t",
            mode="live",
            max_pages=10,
            stop_when_no_new=True,
        )
        assert outcome.n_new == 1
        assert outcome.meta["descending"] is False
        # Second run: page 1 (known) -> page 3 (new) -> page 2 (known, stop).
        assert [routes[n].call_count for n in ("1", "2", "3")] == [2, 2, 2]

    @respx.mock
    async def test_blocked_html_is_recorded_as_failure(self, svc: Services) -> None:
        bse_page({"pageno": "1"}).mock(
            return_value=httpx.Response(200, content=b"<!DOCTYPE html><html>blocked</html>")
        )
        outcome = await ingest.collect_bse(
            svc,
            date(2026, 9, 25),
            date(2026, 9, 25),
            job="t",
            mode="live",
            max_pages=10,
            stop_when_no_new=True,
        )
        assert not outcome.ok and "HTML" in (outcome.error or "")
        assert count(svc, raw_documents, raw_documents.c.kind == "bad_bse_ann") == 1
        assert count(svc, fetch_log, fetch_log.c.ok.is_(False)) == 1

    @respx.mock
    async def test_backfill_uses_event_time_and_keeps_every_page(self, svc: Services) -> None:
        bse_page({"pageno": "1"}).mock(
            return_value=httpx.Response(200, content=bse_payload([bse_row("b1", stamp(1))]))
        )
        await ingest.collect_bse(
            svc,
            date(2026, 9, 25),
            date(2026, 9, 25),
            job="t",
            mode="backfill",
            max_pages=10,
            stop_when_no_new=False,
        )
        await ingest.collect_bse(
            svc,
            date(2026, 9, 25),
            date(2026, 9, 25),
            job="t",
            mode="backfill",
            max_pages=10,
            stop_when_no_new=False,
        )
        with svc.engine.begin() as conn:
            row = conn.execute(select(announcements)).one()
        assert row.ingest_mode == "backfill"
        assert row.available_at == datetime(2026, 9, 25, 4, 31, tzinfo=UTC)
        assert count(svc, raw_documents) == 1  # identical payload deduplicated


class TestJobs:
    @respx.mock
    async def test_first_run_is_catchup_then_live(self, svc: Services) -> None:
        rows = [bse_row("c1", stamp(1))]
        bse_page({"pageno": "1"}).mock(
            side_effect=lambda _req: httpx.Response(
                200, content=bse_payload([dict(r) for r in rows])
            )
        )
        job = BseAnnouncementsJob("bse", 30, 2.0, time(0), time(7))
        await job.run_once(svc)
        rows.insert(0, bse_row("c2", stamp(2)))
        await job.run_once(svc)
        with svc.engine.begin() as conn:
            modes = dict(
                conn.execute(
                    select(announcements.c.source_ann_id, announcements.c.ingest_mode)
                ).all()
            )
        assert modes == {"c1": "catchup", "c2": "live"}

    def test_night_interval(self) -> None:
        job = BseAnnouncementsJob("bse", 30, 2.0, time(0), time(7))
        assert job.interval_s(datetime(2026, 9, 25, 20, 0, tzinfo=UTC)) == 60  # 01:30 IST
        assert job.interval_s(datetime(2026, 9, 25, 6, 0, tzinfo=UTC)) == 30  # 11:30 IST


class TestNse:
    @respx.mock
    async def test_warms_session_then_ingests(self, svc: Services) -> None:
        home = respx.get(NSE_HOME).mock(return_value=httpx.Response(200, text="<html>"))
        respx.get(NSE_URL).mock(
            return_value=httpx.Response(
                200, content=json.dumps([nse_row("s1"), nse_row("s2")]).encode()
            )
        )
        outcome = await ingest.collect_nse(
            svc, date(2026, 9, 25), date(2026, 9, 25), job="t", mode="live"
        )
        assert outcome.ok and outcome.n_new == 2
        assert home.call_count == 1


class TestEod:
    @respx.mock
    async def test_holiday_404_is_ok_and_bounded(self, svc: Services, clock: FakeClock) -> None:
        clock.now = datetime(2026, 9, 25, 14, 0, tzinfo=UTC)  # Fri 19:30 IST
        respx.get(NSE_HOME).mock(return_value=httpx.Response(200))
        respx.get(EOD_URL.format("25092026")).mock(
            return_value=httpx.Response(200, content=EOD_CSV)
        )
        holiday = respx.get(EOD_URL.format("24092026")).mock(return_value=httpx.Response(404))
        job = EodJob("eod", 1800, catchup_days=2, publish_after=time(18), max_missing_attempts=2)

        for _ in range(4):
            await job.run_once(svc)
        assert count(svc, eod_prices) == 2
        assert holiday.call_count == 2  # stops asking after the limit

    async def test_due_days_skips_weekend_and_before_publish(
        self, svc: Services, clock: FakeClock
    ) -> None:
        clock.now = datetime(2026, 9, 28, 6, 0, tzinfo=UTC)  # Mon 11:30 IST
        job = EodJob("eod", 1800, catchup_days=4, publish_after=time(18), max_missing_attempts=3)
        assert job.due_days(svc) == [date(2026, 9, 25)]

    @respx.mock
    async def test_backfill_available_at_is_publish_time(self, svc: Services) -> None:
        respx.get(NSE_HOME).mock(return_value=httpx.Response(200))
        respx.get(EOD_URL.format("25092026")).mock(
            return_value=httpx.Response(200, content=EOD_CSV)
        )
        await ingest.ingest_eod_day(svc, date(2026, 9, 25), job="t", mode="backfill")
        with svc.engine.begin() as conn:
            stamps = set(conn.execute(select(eod_prices.c.available_at)).scalars())
        assert stamps == {datetime(2026, 9, 25, 12, 30, tzinfo=UTC)}


class TestSnapshots:
    @respx.mock
    async def test_daily_window_and_once_per_day(self, svc: Services, clock: FakeClock) -> None:
        respx.get(NSE_HOME).mock(return_value=httpx.Response(200))
        bands = respx.get(svc.settings.nse_bands_url).mock(
            return_value=httpx.Response(200, content=BANDS_CSV)
        )
        respx.get(svc.settings.nse_instruments_url).mock(
            return_value=httpx.Response(200, content=INSTRUMENTS_CSV)
        )
        job = SnapshotJob("bands", "bands", 1800, time(8))

        clock.now = datetime(2026, 9, 25, 2, 0, tzinfo=UTC)  # 07:30 IST
        assert (await job.run_once(svc)).meta["skipped"] == "before daily window"
        clock.now = datetime(2026, 9, 25, 3, 0, tzinfo=UTC)  # 08:30 IST
        await job.run_once(svc)
        await job.run_once(svc)
        assert bands.call_count == 1
        assert count(svc, price_bands) == 2

        await SnapshotJob("inst", "instruments", 1800, time(8)).run_once(svc)
        assert count(svc, instrument_snapshots) == 1


class TestAttachments:
    @respx.mock
    async def test_bse_falls_back_to_history_folder(self, svc: Services) -> None:
        bse_page({"pageno": "1"}).mock(
            return_value=httpx.Response(
                200, content=bse_payload([bse_row("a1", stamp(1)), bse_row("a2", stamp(2))])
            )
        )
        await ingest.collect_bse(
            svc,
            date(2026, 9, 25),
            date(2026, 9, 25),
            job="t",
            mode="live",
            max_pages=1,
            stop_when_no_new=True,
        )
        respx.get(LIVE + "a1.pdf").mock(return_value=httpx.Response(404))
        respx.get(HIST + "a1.pdf").mock(return_value=httpx.Response(200, content=b"%PDF-1"))
        respx.get(LIVE + "a2.pdf").mock(return_value=httpx.Response(404))
        respx.get(HIST + "a2.pdf").mock(return_value=httpx.Response(404))

        outcome = await ingest.fetch_pending_attachments(svc, job="att")
        with svc.engine.begin() as conn:
            status = dict(
                conn.execute(
                    select(announcements.c.source_ann_id, announcements.c.attachment_status)
                ).all()
            )
        assert status == {"a1": "done", "a2": "missing"}
        assert outcome.n_new == 1
        assert count(svc, raw_documents, raw_documents.c.kind == "attachment") == 1


class TestReparse:
    @respx.mock
    async def test_reparse_is_stable(self, svc: Services, clock: FakeClock) -> None:
        bse_page({"pageno": "1"}).mock(
            return_value=httpx.Response(200, content=bse_payload([bse_row("r1", stamp(1))]))
        )
        await ingest.collect_bse(
            svc,
            date(2026, 9, 25),
            date(2026, 9, 25),
            job="t",
            mode="live",
            max_pages=1,
            stop_when_no_new=True,
        )
        with svc.engine.begin() as conn:
            before = conn.execute(select(announcements)).one()
        clock.now += timedelta(days=5)
        stats = ingest.reparse_kind(svc, "bse_ann")
        with svc.engine.begin() as conn:
            after = conn.execute(select(announcements)).one()
        assert stats == {"documents": 1, "updated": 1, "inserted": 0, "errors": 0}
        assert after.available_at == before.available_at
        assert after.first_seen_at == before.first_seen_at

    @respx.mock
    async def test_reparse_covers_payloads_first_stored_by_a_probe(self, svc: Services) -> None:
        # A probe stored these exact bytes first, so the shared raw document
        # keeps kind probe_bse; the backfilled rows must still be reparsed.
        payload = bse_payload([bse_row("p1", stamp(1))], row_count=1)
        with svc.engine.begin() as conn:
            repo.save_raw(
                conn,
                svc.store,
                payload,
                kind="probe_bse",
                source="BSE",
                url="u",
                content_type=None,
                fetched_at=svc.clock(),
            )
        bse_page({"pageno": "1"}).mock(return_value=httpx.Response(200, content=payload))
        await ingest.backfill_day(svc, "BSE", date(2026, 9, 25), job="t", max_pages=5)
        stats = ingest.reparse_kind(svc, "bse_ann")
        assert stats == {"documents": 1, "updated": 1, "inserted": 0, "errors": 0}


class TestNseSubset:
    @respx.mock
    async def test_live_stores_only_new_rows(self, svc: Services) -> None:
        respx.get(NSE_HOME).mock(return_value=httpx.Response(200))
        day_rows = [nse_row("s1")]
        respx.get(NSE_URL).mock(
            side_effect=lambda _req: httpx.Response(200, content=json.dumps(day_rows).encode())
        )
        await ingest.collect_nse(svc, date(2026, 9, 25), date(2026, 9, 25), job="t", mode="live")
        day_rows.insert(0, nse_row("s2", symbol="OTHER"))
        await ingest.collect_nse(svc, date(2026, 9, 25), date(2026, 9, 25), job="t", mode="live")

        with svc.engine.begin() as conn:
            docs = conn.execute(select(raw_documents).order_by(raw_documents.c.first_fetched_at))
            stored = [json.loads(svc.store.get(d.doc_id)) for d in docs]
        assert [[row["seq_id"] for row in doc] for doc in stored] == [["s1"], ["s2"]]

        stats = ingest.reparse_kind(svc, "nse_ann")
        assert stats == {"documents": 2, "updated": 2, "inserted": 0, "errors": 0}


class TestSingleDayQueries:
    @respx.mock
    async def test_bse_job_never_sends_a_range(self, svc: Services, clock: FakeClock) -> None:
        route = respx.get(BSE_URL).mock(
            return_value=httpx.Response(200, content=bse_payload([bse_row("d1", stamp(1))]))
        )
        job = BseAnnouncementsJob("bse", 30, 2.0, time(0), time(7))
        await job.run_once(svc)  # 11:30 IST -> today only
        clock.now = datetime(2026, 9, 25, 19, 0, tzinfo=UTC)  # 00:30 IST on the 26th
        await job.run_once(svc)
        sent = [
            (c.request.url.params["strPrevDate"], c.request.url.params["strToDate"])
            for c in route.calls
        ]
        assert sent == [
            ("20260925", "20260925"),
            ("20260925", "20260925"),
            ("20260926", "20260926"),
        ]

    @respx.mock
    async def test_bse_empty_object_is_not_a_failure(self, svc: Services) -> None:
        respx.get(BSE_URL).mock(return_value=httpx.Response(200, content=b"{}"))
        job = BseAnnouncementsJob("bse", 30, 2.0, time(0), time(7))
        outcome = await job.run_once(svc)
        assert outcome.ok and outcome.n_records == 0


class TestAttachmentPolicy:
    def test_wanted(self) -> None:
        inc, exc = ["order", "result"], ["trading window", "voting result"]
        assert ingest.attachment_wanted(("Company Update", "Receipt of Order", None), inc, exc)
        assert not ingest.attachment_wanted(("Trading Window", None, "Trading Window"), inc, exc)
        assert not ingest.attachment_wanted(("AGM/EGM", "Voting Results", None), inc, exc)
        assert not ingest.attachment_wanted(("Change in Director", None, None), inc, exc)
        assert ingest.attachment_wanted(("Anything", None, None), [], exc)

    @respx.mock
    async def test_skips_by_policy_and_caps_size(self, svc: Services) -> None:
        rows = [
            bse_row("big", stamp(3)),
            bse_row(
                "noise",
                stamp(2),
                SUBCATNAME="Trading Window",
                NEWSSUB="Trading Window",
                CATEGORYNAME="Insider Trading / SAST",
            ),
            bse_row("small", stamp(1)),
        ]
        respx.get(BSE_URL).mock(return_value=httpx.Response(200, content=bse_payload(rows)))
        await ingest.collect_bse(
            svc,
            date(2026, 9, 25),
            date(2026, 9, 25),
            job="t",
            mode="live",
            max_pages=1,
            stop_when_no_new=True,
        )
        svc.settings.attachments_max_mb = 0.001  # ~1 KB
        big = respx.get(LIVE + "big.pdf").mock(
            return_value=httpx.Response(200, content=b"%PDF" + b"0" * 5000)
        )
        respx.get(LIVE + "small.pdf").mock(return_value=httpx.Response(200, content=b"%PDF-1"))
        noise = respx.get(LIVE + "noise.pdf").mock(return_value=httpx.Response(200))

        outcome = await ingest.fetch_pending_attachments(svc, job="att")
        with svc.engine.begin() as conn:
            status = dict(
                conn.execute(
                    select(announcements.c.source_ann_id, announcements.c.attachment_status)
                ).all()
            )
            attempts = dict(
                conn.execute(
                    select(announcements.c.source_ann_id, announcements.c.attachment_attempts)
                ).all()
            )
        assert status == {"big": "too_large", "noise": "skipped", "small": "done"}
        assert noise.call_count == 0 and big.call_count == 1
        assert attempts["noise"] == 0
        assert outcome.meta["skipped"] == 1
