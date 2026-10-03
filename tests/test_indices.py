"""NSE index closes (T2.7)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import func, select

from gats import ingest
from gats.db import repo
from gats.db.schema import index_days, index_eod
from gats.ingest import Services
from gats.pit import AsOf
from gats.recorder import EodJob
from gats.sources import nse_indices
from gats.sources.models import PayloadError
from tests.conftest import NSE_HOME, FakeClock

REAL = Path(__file__).parent / "fixtures" / "real" / "nse_indices_2026-10-01.csv"
URL = "https://nsearchives.nseindia.com/content/indices/ind_close_all_{}.csv"
DAY = date(2026, 10, 1)


class TestParser:
    def test_real_file(self) -> None:
        parsed = nse_indices.parse_index_close(REAL.read_bytes(), trade_date=DAY)
        assert parsed.warnings == []
        assert len(parsed.records) == 11
        by_name = {r.index_name: r for r in parsed.records}
        n500 = by_name["Nifty 500"]
        assert (n500.open, n500.high, n500.low, n500.close) == (
            22005.0,
            22046.8,
            21623.25,
            21857.75,
        )
        assert (n500.pct_change, n500.volume, n500.turnover_cr) == (-0.97, 2742935137, 96078.59)
        # "-" means not applicable (VIX has no volume or valuation ratios).
        vix = by_name["India VIX"]
        assert vix.close == 14.46 and vix.volume is None and vix.pe is None
        points = by_name["Nifty50 Dividend Points"]
        assert points.open is None and points.close == 196.79

    def test_rows_dated_another_day_are_dropped(self) -> None:
        parsed = nse_indices.parse_index_close(REAL.read_bytes(), trade_date=date(2026, 10, 2))
        assert parsed.records == []
        assert parsed.meta["dates_seen"] == ["2026-10-01"]

    def test_missing_columns_and_html(self) -> None:
        with pytest.raises(PayloadError, match="missing columns"):
            nse_indices.parse_index_close(b"A,B\n1,2\n", trade_date=DAY)
        with pytest.raises(PayloadError, match="HTML"):
            nse_indices.parse_index_close(b"<!DOCTYPE html><html>", trade_date=DAY)


class TestIngest:
    @respx.mock
    async def test_loaded_and_holiday(self, svc: Services) -> None:
        respx.get(NSE_HOME).mock(return_value=httpx.Response(200))
        respx.get(URL.format("01102026")).mock(
            return_value=httpx.Response(200, content=REAL.read_bytes())
        )
        respx.get(URL.format("02102026")).mock(return_value=httpx.Response(404))
        loaded = await ingest.ingest_index_day(svc, DAY, job="t", mode="backfill")
        holiday = await ingest.ingest_index_day(svc, date(2026, 10, 2), job="t", mode="backfill")
        assert (loaded.ok, loaded.n_records) == (True, 11)
        assert holiday.ok and holiday.http_status == 404
        with svc.engine.begin() as conn:
            assert conn.execute(select(func.count()).select_from(index_eod)).scalar_one() == 11
            loaded_row = repo.eod_day(conn, DAY, index_days)
            holiday_row = repo.eod_day(conn, date(2026, 10, 2), index_days)
            # Backfilled closes are dated at the EOD publication time (18:00 IST).
            stamps = set(conn.execute(select(index_eod.c.available_at)).scalars())
        assert loaded_row is not None and loaded_row.status == "loaded"
        assert holiday_row is not None and holiday_row.status == "not_published"
        assert stamps == {datetime(2026, 10, 1, 12, 30, tzinfo=UTC)}

    @respx.mock
    async def test_job_fetches_once_and_reparse_is_stable(
        self, svc: Services, clock: FakeClock
    ) -> None:
        clock.now = datetime(2026, 10, 1, 13, 0, tzinfo=UTC)  # Thu 18:30 IST
        respx.get(NSE_HOME).mock(return_value=httpx.Response(200))
        route = respx.get(URL.format("01102026")).mock(
            return_value=httpx.Response(200, content=REAL.read_bytes())
        )
        job = EodJob("nse_indices", 1800, 1, time(18), 3, spec=ingest.INDEX_FILE)
        await job.run_once(svc)
        await job.run_once(svc)
        assert route.call_count == 1
        stats = ingest.reparse_kind(svc, nse_indices.KIND)
        assert stats == {"documents": 1, "updated": 11, "inserted": 0, "errors": 0}


@respx.mock
async def test_asof_index_history_is_point_in_time(svc: Services) -> None:
    respx.get(NSE_HOME).mock(return_value=httpx.Response(200))
    respx.get(URL.format("01102026")).mock(
        return_value=httpx.Response(200, content=REAL.read_bytes())
    )
    await ingest.ingest_index_day(svc, DAY, job="t", mode="backfill")
    with svc.engine.begin() as conn:
        # 1 Oct 17:00 IST: the close existed but this system could not know it yet.
        before = AsOf(conn, datetime(2026, 10, 1, 11, 30, tzinfo=UTC))
        after = AsOf(conn, datetime(2026, 10, 1, 12, 30, tzinfo=UTC))
        assert before.index_history("Nifty 500") == []
        assert [r.close for r in after.index_history("Nifty 500")] == [21857.75]
