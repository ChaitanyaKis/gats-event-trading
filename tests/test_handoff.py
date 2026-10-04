"""The recorder's fast lane (T7.1): a new filing a strategy trades is typed,
linked and read at once, not on the batch jobs' timers. No network: respx."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import respx
from sqlalchemy import select

from gats.db import repo
from gats.db.schema import (
    announcement_event_types,
    announcement_security,
    announcements,
    document_texts,
)
from gats.ingest import Outcome, Services
from gats.recorder import (
    HandOffJob,
    NseAnnouncementsJob,
    build_jobs,
    run_job_loop,
    sleep_or_stop,
)
from gats.refdata import master
from gats.sources.models import AnnouncementRecord, InstrumentRecord
from gats.timeutil import to_ist

PDF = (Path(__file__).parent / "fixtures" / "real" / "order_win_attachment.pdf").read_bytes()
ORDER = "Bagging/Receiving of orders/contracts"
BASE = "https://nsearchives.nseindia.com/corporate/"


def filing(
    svc: Services,
    n: int,
    *,
    seen: datetime,
    source: str = "NSE",
    category: str | None = ORDER,
    subcategory: str | None = None,
    mode: repo.IngestMode = "live",
    symbol: str = "AAA",
) -> int:
    record = AnnouncementRecord(
        source=source,
        source_ann_id=str(n),
        symbol=symbol if source == "NSE" else None,
        scrip_code=None if source == "NSE" else "500001",
        isin=None,
        company_name="A Ltd",
        category=category,
        subcategory=subcategory,
        subject=category or subcategory,
        details=None,
        attachment_url=f"{BASE}{n}.pdf",
        exch_submitted_ts=None,
        exch_disseminated_ts=seen,
        event_ts=seen,
    )
    with svc.engine.begin() as conn:
        page = repo.save_raw(
            conn, svc.store, b"page", kind="t", source=source, url="u", content_type=None,
            fetched_at=seen,
        )  # fmt: skip
        repo.insert_announcements(
            conn, [record], raw_doc_id=page, parser_version="v", mode=mode, fetched_at=seen,
            now=seen,
        )  # fmt: skip
        return int(
            conn.execute(
                select(announcements.c.id).where(
                    announcements.c.source == source, announcements.c.source_ann_id == str(n)
                )
            ).scalar_one()
        )


def hand_off(svc: Services) -> HandOffJob:
    (job,) = [j for j in build_jobs(svc) if isinstance(j, HandOffJob)]
    return job


def state(svc: Services) -> dict[int, tuple[str | None, str, bool]]:
    """announcement id -> (event type, attachment status, has text)."""
    a, et, t = announcements, announcement_event_types, document_texts
    with svc.engine.begin() as conn:
        rows = conn.execute(
            select(a.c.id, et.c.event_type, a.c.attachment_status, t.c.doc_id)
            .select_from(
                a.outerjoin(et, et.c.announcement_id == a.c.id).outerjoin(
                    t, t.c.doc_id == a.c.attachment_doc_id
                )
            )
            .order_by(a.c.id)
        ).all()
    return {row.id: (row.event_type, row.attachment_status, row.doc_id is not None) for row in rows}


@respx.mock
async def test_a_new_order_win_is_typed_linked_and_read_in_one_pass(svc: Services) -> None:
    now = svc.clock()
    with svc.engine.begin() as conn:
        page = repo.save_raw(
            conn, svc.store, b"list", kind="t", source="NSE", url="u", content_type=None,
            fetched_at=now,
        )  # fmt: skip
        repo.upsert_instruments(
            conn,
            to_ist(now).date(),
            [InstrumentRecord("AAA", "EQ", "INE00001A0101", "A Ltd", None, 10.0, 1)],
            raw_doc_id=page,
            parser_version="v",
            available_at=now - timedelta(days=1),
        )
        master.build(conn, now - timedelta(days=1))
    order = filing(svc, 1, seen=now)
    meeting = filing(svc, 2, seen=now, category="Board Meeting Intimation")
    on_bse = filing(svc, 3, seen=now, source="BSE", category="Company Update",
                    subcategory="Award of Order / Receipt of Order")  # fmt: skip
    old = filing(svc, 4, seen=now - timedelta(hours=2))
    backfilled = filing(svc, 5, seen=now, mode="backfill")
    home = respx.get(svc.settings.nse_home_url).mock(return_value=httpx.Response(200))
    pdf = respx.get(f"{BASE}1.pdf").mock(return_value=httpx.Response(200, content=PDF))

    job = hand_off(svc)
    job.wake.set()
    outcome = await job.run_once(svc)

    assert not job.wake.is_set()
    assert outcome.ok and outcome.n_records == 3 and outcome.n_new == 1
    assert outcome.meta == {"in_scope": 1, "attachments": 1, "texts": 1, "retrying": 0}
    got = state(svc)
    assert got[order] == ("ORDER_WIN", "done", True)
    assert got[meeting][0] not in (None, "ORDER_WIN") and got[meeting][1:] == ("pending", False)
    assert got[on_bse] == ("ORDER_WIN", "pending", False)  # typed, but only NSE is fetched ahead
    assert got[old] == (None, "pending", False)  # too old: the batch jobs' work
    assert got[backfilled] == (None, "pending", False)  # history, not news
    with svc.engine.begin() as conn:
        linked = dict(
            conn.execute(
                select(announcement_security.c.announcement_id, announcement_security.c.method)
            ).all()
        )
    assert linked[order] == "nse_symbol" and old not in linked
    assert (home.call_count, pdf.call_count) == (1, 1)

    again = await job.run_once(svc)  # nothing new: no queries to the exchange, nothing redone
    assert again.n_records == 0 and again.meta["in_scope"] == 0
    assert (home.call_count, pdf.call_count) == (1, 1)


@respx.mock
async def test_a_failed_attachment_is_tried_again_on_the_next_pass(svc: Services) -> None:
    now = svc.clock()
    order = filing(svc, 1, seen=now)
    respx.get(svc.settings.nse_home_url).mock(return_value=httpx.Response(200))
    pdf = respx.get(f"{BASE}1.pdf").mock(
        side_effect=[*[httpx.Response(503)] * 3, httpx.Response(200, content=PDF)]
    )
    job = hand_off(svc)
    first = await job.run_once(svc)
    assert first.ok and first.meta == {"in_scope": 1, "attachments": 0, "texts": 0, "retrying": 1}
    assert state(svc)[order] == ("ORDER_WIN", "failed", False)
    second = await job.run_once(svc)
    assert second.n_records == 0 and second.meta["texts"] == 1 and second.meta["retrying"] == 0
    assert state(svc)[order] == ("ORDER_WIN", "done", True)
    assert pdf.call_count == 4  # the client gave up after three tries; the next pass got it


async def test_new_filings_wake_the_hand_off_at_once(svc: Services) -> None:
    jobs = build_jobs(svc)
    job = hand_off(svc)
    (nse,) = [j for j in jobs if isinstance(j, NseAnnouncementsJob)]
    assert nse.notify is not None and not nse.notify.is_set()
    nse._mark(Outcome(ok=True, n_new=0))
    assert not nse.notify.is_set()  # nothing new: nobody is woken
    nse._mark(Outcome(ok=True, n_new=2))
    assert nse.notify.is_set()

    stop = asyncio.Event()
    await asyncio.wait_for(sleep_or_stop(stop, 3600, nse.notify), 1)  # woken, not timed out
    stop.set()
    await asyncio.wait_for(sleep_or_stop(stop, 3600, asyncio.Event()), 1)  # stopping also ends it
    await asyncio.wait_for(sleep_or_stop(asyncio.Event(), 0.01, asyncio.Event()), 1)  # the timer
    assert job.interval_s(svc.clock()) == svc.settings.handoff_fallback_s


async def test_the_job_loop_runs_a_woken_job_again_without_waiting(svc: Services) -> None:
    class Woken:
        name = "woken"

        def __init__(self) -> None:
            self.wake = asyncio.Event()
            self.runs = 0

        def interval_s(self, now: datetime) -> float:
            return 3600.0

        async def run_once(self, svc: Services) -> Outcome:
            self.runs += 1
            if self.runs == 1:
                self.wake.set()  # a filing arrived while the job was running
            else:
                stop.set()
            return Outcome(ok=True)

    from gats.recorder import Heartbeat

    stop = asyncio.Event()
    job = Woken()
    heartbeat = Heartbeat(svc.settings.heartbeat_path)
    await asyncio.wait_for(run_job_loop(job, svc, stop, heartbeat, max_backoff_s=1), 5)
    assert job.runs == 2


def test_hand_off_can_be_switched_off(svc: Services) -> None:
    svc.settings.handoff_enabled = False
    jobs = build_jobs(svc)
    assert not any(isinstance(j, HandOffJob) for j in jobs)
    (nse,) = [j for j in jobs if isinstance(j, NseAnnouncementsJob)]
    assert nse.notify is None
