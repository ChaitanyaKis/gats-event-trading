"""Quarterly results ingestion and point-in-time trailing revenue (T4.7)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import Connection, Engine, select

from gats.backtest.feed import with_revenue_ratio
from gats.db.schema import financial_results
from gats.ingest import Services
from gats.marketdata.windows import EventWindow
from gats.pit import AsOf
from gats.rawstore import RawStore
from gats.refdata.results import fetch_pending_xbrl, ingest_results_index
from gats.timeutil import ist_datetime
from tests.test_event_study import Market

REAL = Path(__file__).parent / "fixtures" / "real"
LEGACY = (REAL / "nse_financial_results_RELIANCE_2026-10-03.json").read_bytes()
INTEGRATED = (REAL / "nse_integrated_filing_RELIANCE_2026-10-03.json").read_bytes()
XBRL_OLD = (REAL / "nse_xbrl_results_RELIANCE_2024-12-31_consolidated.xml").read_bytes()
XBRL_NEW = (REAL / "nse_xbrl_integrated_RELIANCE_2026-06-30_consolidated.xml").read_bytes()
XBRL = "https://nsearchives.nseindia.com/corporate/xbrl/"
OLD_CONSOLIDATED = XBRL + "INDAS_117297_1348248_16012025081520.xml"
OLD_STANDALONE = XBRL + "INDAS_117298_1348254_16012025082021.xml"
NEW_CONSOLIDATED = XBRL + "INTEGRATED_FILING_INDAS_1695741_17072026075004_WEB.xml"
CRORE = 1e7


def mock_nse(svc: Services) -> None:
    s = svc.settings
    respx.get(s.nse_home_url).mock(return_value=httpx.Response(200))
    respx.get(s.nse_results_url).mock(return_value=httpx.Response(200, content=LEGACY))
    respx.get(s.nse_integrated_results_url).mock(
        return_value=httpx.Response(200, content=INTEGRATED)
    )
    respx.get(OLD_CONSOLIDATED).mock(return_value=httpx.Response(200, content=XBRL_OLD))
    respx.get(NEW_CONSOLIDATED).mock(return_value=httpx.Response(200, content=XBRL_NEW))
    # The standalone filing's link serves the consolidated file: a mismatch.
    respx.get(OLD_STANDALONE).mock(return_value=httpx.Response(200, content=XBRL_OLD))
    respx.get(url__startswith=XBRL).mock(return_value=httpx.Response(404))


def stored(svc: Services) -> list[Any]:
    with svc.engine.begin() as conn:
        return list(conn.execute(select(financial_results)).all())


@respx.mock
async def test_index_rows_from_both_regimes_are_stored_once(svc: Services) -> None:
    mock_nse(svc)
    first = await ingest_results_index(svc, "RELIANCE", job="t")
    assert first.ok and (first.n_records, first.n_new) == (18, 18)  # 6 legacy + 12 integrated
    again = await ingest_results_index(svc, "RELIANCE", job="t")
    assert (again.n_records, again.n_new) == (18, 0)  # idempotent
    rows = stored(svc)
    assert {r.regime for r in rows} == {"legacy", "integrated"}
    assert all(r.available_at == r.event_ts and r.xbrl_status == "pending" for r in rows)
    assert min(r.period_end for r in rows) == date(2024, 6, 30)  # the 2005 rows had no time


@respx.mock
async def test_revenue_is_read_only_from_a_file_that_matches_its_filing(svc: Services) -> None:
    mock_nse(svc)
    await ingest_results_index(svc, "RELIANCE", job="t")
    stats = await fetch_pending_xbrl(svc, since=date(2024, 10, 1), limit=50, job="t")
    assert (stats.attempted, stats.done, stats.failed) == (7, 2, 5)  # consolidated filings only
    by_url = {r.xbrl_url: r for r in stored(svc)}
    assert by_url[OLD_CONSOLIDATED].revenue == 2_438_650_000_000.0
    assert by_url[NEW_CONSOLIDATED].revenue == 3_118_500_000_000.0
    assert sum(r.xbrl_note == "HTTP 404" for r in by_url.values()) == 5
    skipped = [r for r in by_url.values() if not r.consolidated or r.period_end < date(2024, 10, 1)]
    assert skipped and all(
        r.xbrl_attempts == 0 for r in skipped
    )  # standalone twins, older quarters

    retry = await fetch_pending_xbrl(svc, since=date(2024, 10, 1), limit=50, job="t")
    assert (retry.attempted, retry.done) == (5, 0)  # the two that worked are not asked again
    await fetch_pending_xbrl(svc, since=date(2024, 10, 1), limit=50, job="t")
    final = await fetch_pending_xbrl(svc, since=date(2024, 10, 1), limit=50, job="t")
    assert final.attempted == 0  # three attempts each, then left alone


@respx.mock
async def test_a_file_that_is_not_its_filing_is_refused(svc: Services) -> None:
    """A standalone filing with no consolidated twin is read, and here its
    link serves a consolidated file: the mismatch is caught, not stored."""
    mock_nse(svc)
    await ingest_results_index(svc, "RELIANCE", job="t")
    with svc.engine.begin() as conn:
        conn.execute(
            financial_results.delete().where(
                financial_results.c.period_end == date(2024, 12, 31),
                financial_results.c.consolidated.is_(True),
            )
        )
    await fetch_pending_xbrl(svc, since=date(2024, 12, 1), limit=50, job="t")
    wrong = next(r for r in stored(svc) if r.xbrl_url == OLD_STANDALONE)
    assert wrong.xbrl_status == "failed" and wrong.revenue is None
    assert "standalone/consolidated" in wrong.xbrl_note


# --- trailing revenue, point in time ------------------------------------------------------

NOW = datetime(2024, 4, 1, tzinfo=UTC)


def quarter(conn: Connection, doc: str, end: date, crore: float, public: date,
            consolidated: bool = True, seq: str = "1") -> None:  # fmt: skip
    shown = ist_datetime(public, time(18, 0))
    conn.execute(
        financial_results.insert().values(
            symbol="AAA", period_end=end, consolidated=consolidated, seq=seq, regime="legacy",
            audited=False, revised=seq != "1", xbrl_status="done", xbrl_attempts=1,
            revenue=crore * CRORE, event_ts=shown, available_at=shown, raw_doc_id=doc,
            parser_version="v",
        )
    )  # fmt: skip


@pytest.fixture
def company(engine: Engine, store: RawStore) -> Iterator[tuple[Connection, int, str]]:
    """AAA with five consolidated quarters, each public about 40 days after it ended."""
    with engine.begin() as conn:
        market = Market(conn, store)
        market.list_stocks(["AAA"])
        market.write_prices(["AAA"])
        doc = market.doc()
        for end, crore, public in [
            (date(2022, 12, 31), 90, date(2023, 2, 10)),
            (date(2023, 3, 31), 100, date(2023, 5, 10)),
            (date(2023, 6, 30), 110, date(2023, 8, 10)),
            (date(2023, 9, 30), 120, date(2023, 11, 10)),
            (date(2023, 12, 31), 130, date(2024, 2, 10)),
        ]:
            quarter(conn, doc, end, crore, public)
        security = AsOf(conn, NOW).resolver().resolve("nse_symbol", "AAA", date(2024, 1, 10))
        assert security is not None
        yield conn, security, doc


def at(day: date) -> datetime:
    return ist_datetime(day, time(11, 0))


def test_trailing_revenue_uses_only_results_already_public(
    company: tuple[Connection, int, str],
) -> None:
    conn, security, _ = company
    clock = AsOf(conn, NOW)
    after = clock.trailing_revenue(security, at(date(2024, 2, 15)))
    assert after is not None and after.rupees == 460 * CRORE and after.consolidated
    assert after.quarters[0] == date(2023, 12, 31) and len(after.quarters) == 4
    before = clock.trailing_revenue(security, at(date(2024, 2, 1)))  # December not out yet
    assert before is not None and before.rupees == 420 * CRORE
    assert before.known_at == ist_datetime(date(2023, 11, 10), time(18, 0))
    early = AsOf(conn, at(date(2024, 2, 1)))  # the clock is earlier than the moment asked
    capped = early.trailing_revenue(security, at(date(2024, 3, 1)))
    assert capped is not None and capped.rupees == 420 * CRORE
    assert clock.trailing_revenue(security, at(date(2023, 6, 1))) is None  # only two quarters known


def test_a_revision_counts_from_when_it_is_public(company: tuple[Connection, int, str]) -> None:
    conn, security, doc = company
    quarter(conn, doc, date(2023, 12, 31), 135, date(2024, 3, 1), seq="2")
    clock = AsOf(conn, NOW)
    assert clock.trailing_revenue(security, at(date(2024, 2, 20))).rupees == 460 * CRORE  # type: ignore[union-attr]
    assert clock.trailing_revenue(security, at(date(2024, 3, 5))).rupees == 465 * CRORE  # type: ignore[union-attr]


def test_gaps_and_stale_figures_give_no_answer(company: tuple[Connection, int, str]) -> None:
    conn, security, _ = company
    clock = AsOf(conn, datetime(2026, 1, 1, tzinfo=UTC))
    assert clock.trailing_revenue(security, at(date(2025, 6, 1))) is None  # newest is 17 months old
    conn.execute(
        financial_results.delete().where(financial_results.c.period_end == date(2023, 6, 30))
    )
    assert (
        AsOf(conn, NOW).trailing_revenue(security, at(date(2024, 2, 15))) is None
    )  # a missing quarter


def test_standalone_is_used_when_there_is_no_consolidated_run(
    engine: Engine, store: RawStore
) -> None:
    with engine.begin() as conn:
        market = Market(conn, store)
        market.list_stocks(["AAA"])
        market.write_prices(["AAA"])
        doc = market.doc()
        for n, end in enumerate(
            [date(2023, 3, 31), date(2023, 6, 30), date(2023, 9, 30), date(2023, 12, 31)]
        ):
            quarter(conn, doc, end, 10, date(2024, 2, 1 + n), consolidated=False)
        quarter(conn, doc, date(2023, 12, 31), 500, date(2024, 2, 10), consolidated=True)
        clock = AsOf(conn, NOW)
        security = clock.resolver().resolve("nse_symbol", "AAA", date(2024, 1, 10))
        assert security is not None
        found = clock.trailing_revenue(security, at(date(2024, 2, 15)))
    assert found is not None and (found.rupees, found.consolidated) == (40 * CRORE, False)


def test_amount_vs_revenue_joins_facts_with_what_was_known(
    company: tuple[Connection, int, str],
) -> None:
    conn, security, _ = company
    clock = AsOf(conn, NOW)
    day = date(2024, 2, 15)
    known = EventWindow(1, "ORDER_WIN", security, "NSE_EQ|X", at(day), day, (day, day, day))
    unsized = EventWindow(2, "ORDER_WIN", security, "NSE_EQ|X", at(day), day, (day, day, day))
    facts = with_revenue_ratio(
        clock, [known, unsized], {1: {"amount_inr": 46 * CRORE}, 2: {"counterparty": "X"}}
    )
    assert facts[1]["amount_vs_revenue"] == pytest.approx(0.1)
    assert facts[1]["trailing_revenue_rs"] == 460 * CRORE
    assert facts[2] == {"counterparty": "X"}  # no amount: no ratio, nothing invented
