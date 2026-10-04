"""The recorder's results refresh (M7): which companies it asks about, and
that it does not keep asking. The fetching itself is tested in
test_results_ingest.py; here it is replaced by a recorder of calls."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from gats import recorder
from gats.db import repo
from gats.db.schema import announcement_event_types, announcements, financial_results
from gats.ingest import Outcome, Services
from gats.recorder import ResultsJob, build_jobs
from gats.refdata.results import XbrlStats
from gats.research.taxonomy import Taxonomy
from gats.sources.models import AnnouncementRecord
from tests.conftest import FakeClock


def job_of(svc: Services) -> ResultsJob:
    (job,) = [j for j in build_jobs(svc) if isinstance(j, ResultsJob)]
    return job


def version(svc: Services) -> str:
    return Taxonomy.load(svc.settings.taxonomy_path).version


def filing(
    svc: Services, n: int, symbol: str, event_type: str, seen: datetime, source: str = "NSE"
) -> None:
    """A typed filing, first seen at ``seen``."""
    record = AnnouncementRecord(
        source=source, source_ann_id=str(n), symbol=symbol, scrip_code=None, isin=None,
        company_name=symbol, category=event_type, subcategory=None, subject=event_type,
        details=None, attachment_url=None, exch_submitted_ts=None, exch_disseminated_ts=seen,
        event_ts=seen,
    )  # fmt: skip
    with svc.engine.begin() as conn:
        page = repo.save_raw(
            conn, svc.store, f"p{n}".encode(), kind="t", source=source, url="u",
            content_type=None, fetched_at=seen,
        )  # fmt: skip
        repo.insert_announcements(
            conn, [record], raw_doc_id=page, parser_version="v", mode="live", fetched_at=seen,
            now=seen,
        )  # fmt: skip
        ann_id = conn.execute(
            select(announcements.c.id).where(
                announcements.c.source == source, announcements.c.source_ann_id == str(n)
            )
        ).scalar_one()
        conn.execute(
            announcement_event_types.insert().values(
                announcement_id=ann_id, taxonomy_version=version(svc), event_type=event_type,
                rule_no=0, classified_at=seen,
            )
        )  # fmt: skip


def results_stored(svc: Services, symbol: str, shown: datetime) -> None:
    with svc.engine.begin() as conn:
        page = repo.save_raw(
            conn, svc.store, f"r{symbol}{shown}".encode(), kind="t", source="NSE", url="u",
            content_type=None, fetched_at=shown,
        )  # fmt: skip
        conn.execute(
            financial_results.insert().values(
                symbol=symbol, period_end=date(2026, 6, 30), consolidated=True, seq=str(shown),
                regime="legacy", audited=False, revised=False, xbrl_status="done",
                xbrl_attempts=1, revenue=1e9, event_ts=shown, available_at=shown,
                raw_doc_id=page, parser_version="v",
            )
        )  # fmt: skip


def test_it_asks_about_companies_that_reported_or_are_unknown(svc: Services) -> None:
    now = svc.clock()
    long_ago, hour = now - timedelta(days=60), timedelta(hours=1)
    # In the universe (an order win before), and its results came out today.
    filing(svc, 1, "AAA", "ORDER_WIN", long_ago)
    filing(svc, 2, "AAA", "RESULTS", now - 3 * hour)
    results_stored(svc, "AAA", long_ago)  # what is stored is older than the new filing
    # Reported today, but never had a filing a strategy trades: not asked.
    filing(svc, 3, "BBB", "RESULTS", now - hour)
    # A first order win, and nothing stored about the company: asked.
    filing(svc, 4, "CCC", "ORDER_WIN", now - 2 * hour)
    # In the universe and reported, but its new results are already stored.
    filing(svc, 5, "DDD", "ORDER_WIN", long_ago)
    filing(svc, 6, "DDD", "RESULTS", now - 5 * hour)
    results_stored(svc, "DDD", now - 4 * hour)
    # An order win today for a company whose results are known: nothing to do.
    filing(svc, 7, "EEE", "ORDER_WIN", now - hour)
    results_stored(svc, "EEE", long_ago)
    # Results filed a week ago: outside the look-back.
    filing(svc, 8, "FFF", "ORDER_WIN", long_ago)
    filing(svc, 9, "FFF", "RESULTS", now - timedelta(days=7))
    # BSE's copy of a filing does not count: results are asked for by NSE symbol.
    filing(svc, 10, "GGG", "ORDER_WIN", now - hour, source="BSE")

    job = job_of(svc)
    assert job.due(svc, version(svc)) == ["CCC", "AAA"]  # the most recently filed first
    job.max_symbols = 1
    assert job.due(svc, version(svc)) == ["CCC"]


async def test_a_company_is_not_asked_again_until_the_retry_time(
    svc: Services, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = svc.clock()
    filing(svc, 1, "AAA", "ORDER_WIN", now - timedelta(days=60))
    filing(svc, 2, "AAA", "RESULTS", now - timedelta(hours=1))
    asked: list[str] = []
    xbrl: list[dict[str, Any]] = []

    async def index(svc: Services, symbol: str, *, job: str) -> Outcome:
        asked.append(symbol)
        return Outcome(ok=True, n_records=4, n_new=1)

    async def read_xbrl(svc: Services, **kw: Any) -> XbrlStats:
        xbrl.append(kw)
        return XbrlStats(attempted=1, done=1)

    monkeypatch.setattr(recorder, "ingest_results_index", index)
    monkeypatch.setattr(recorder, "fetch_pending_xbrl", read_xbrl)
    job = job_of(svc)
    first = await job.run_once(svc)
    assert first.ok and asked == ["AAA"] and (first.n_records, first.n_new) == (4, 1)
    assert first.meta == {"asked": ["AAA"], "xbrl": {"read": 1, "done": 1}}
    assert xbrl[0]["limit"] == svc.settings.results_max_xbrl and xbrl[0]["job"] == "results"

    # The index did not show the new quarter yet (nothing newer got stored):
    # the company is still due, but not asked again every half hour.
    quiet = await job.run_once(svc)
    assert quiet.meta == {"asked": []} and asked == ["AAA"] and len(xbrl) == 1
    clock.now = now + timedelta(seconds=svc.settings.results_retry_s)
    await job.run_once(svc)
    assert asked == ["AAA", "AAA"]

    results_stored(svc, "AAA", now)  # now it is stored: nothing left to ask
    clock.now = now + timedelta(seconds=3 * svc.settings.results_retry_s)
    assert (await job.run_once(svc)).meta == {"asked": []}


def test_the_job_can_be_switched_off(svc: Services) -> None:
    svc.settings.results_enabled = False
    assert not any(isinstance(j, ResultsJob) for j in build_jobs(svc))
