"""Upstox market data (T5.1): instrument file, candles, bar storage, fetching.

The instrument file fixture is real (trimmed, 2026-10-03). Candle payloads
here are SYNTHETIC, shaped exactly like the example in Upstox's V3 docs:
no real reply exists until the Analytics Token does (HUMAN step).
"""

from __future__ import annotations

import gzip
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr
from sqlalchemy import select
from typer.testing import CliRunner

from gats.cli import app
from gats.db import repo
from gats.db.schema import bar_months, instrument_snapshots, raw_documents
from gats.ingest import Services
from gats.marketdata.bars import (
    eod_check,
    month_path,
    months,
    read_bars,
    read_month,
    summarize,
    write_month,
)
from gats.marketdata.upstox import TokenMissing, fetch_month, key_for_symbol
from gats.sources.models import PayloadError
from gats.sources.upstox import Bar, candles_url, equity_key, parse_candles, parse_instruments

REAL = Path(__file__).parent / "fixtures" / "real" / "upstox_NSE_instruments_2026-10-03.json"
KEY = "NSE_EQ|INE002A01018"  # RELIANCE
IST = "+05:30"


def candle(
    minute: str, o: float = 100, h: float = 101, low: float = 99, c: float = 100.5
) -> list[Any]:
    return [f"{minute}{IST}", o, h, low, c, 1000, 0]


def reply(*candles: list[Any]) -> bytes:
    return json.dumps({"status": "success", "data": {"candles": list(candles)}}).encode()


class TestInstruments:
    @pytest.mark.parametrize("gzipped", [False, True])
    def test_real_file_keeps_cash_equities_and_indices(self, gzipped: bool) -> None:
        payload = REAL.read_bytes()
        result = parse_instruments(gzip.compress(payload) if gzipped else payload)
        assert result.meta == {"rows": 10, "kept": 8, "other_segments": 2}
        by_symbol = {r.trading_symbol: r for r in result.records}
        reliance = by_symbol["RELIANCE"]
        assert reliance.instrument_key == KEY == equity_key("INE002A01018")
        assert (reliance.isin, reliance.instrument_type, reliance.lot_size) == (
            "INE002A01018",
            "EQ",
            1,
        )
        assert reliance.tick_size == 10.0  # as published (paise, apparently)
        assert by_symbol["ETERNAL"].isin == "INE758T01015"  # the key survives ZOMATO -> ETERNAL
        assert by_symbol["NIFTY"].instrument_key == "NSE_INDEX|Nifty 50"
        assert {r.instrument_type for r in result.records} >= {"EQ", "BE", "SM", "SG", "INDEX"}

    @pytest.mark.parametrize(
        "payload", [b"<html>blocked</html>", b'{"segment": "NSE_EQ"}', b"\x1f\x8bnot gzip"]
    )
    def test_not_an_instrument_file(self, payload: bytes) -> None:
        with pytest.raises(PayloadError):
            parse_instruments(payload)


class TestCandles:
    def test_url_matches_the_docs_example(self) -> None:
        url = candles_url(
            "https://api.upstox.com", "NSE_EQ|INE848E01016", "minutes", 1,
            date(2025, 1, 2), date(2025, 1, 1),
        )  # fmt: skip
        assert url == (
            "https://api.upstox.com/v3/historical-candle/NSE_EQ%7CINE848E01016/minutes/1/"
            "2025-01-02/2025-01-01"
        )

    def test_bars_are_utc_and_oldest_first(self) -> None:
        result = parse_candles(
            reply(candle("2026-10-01T09:16:00"), candle("2026-10-01T09:15:00")),
            instrument_key=KEY,
        )
        assert [b.ts for b in result.records] == [
            datetime(2026, 10, 1, 3, 45, tzinfo=UTC),
            datetime(2026, 10, 1, 3, 46, tzinfo=UTC),
        ]
        assert result.records[0].volume == 1000 and not result.warnings

    def test_bad_candles_are_reported_not_stored(self) -> None:
        result = parse_candles(
            reply(
                candle("2026-10-01T09:15:00", h=99, low=101),  # high below low
                ["2026-10-01T09:16:00", 1, 2],  # too short
                ["2026-10-01T09:17:00", 1, 2, 1, 1, 5, 0],  # no offset
                candle("2026-10-01T09:18:00"),
            ),
            instrument_key=KEY,
        )
        assert len(result.records) == 1 and len(result.warnings) == 3

    @pytest.mark.parametrize(
        "payload",
        [
            b'{"status": "error", "errors": [{"errorCode": "UDAPI100050"}]}',
            b'{"status": "success", "data": {}}',
            b"<html>gateway timeout</html>",
        ],
    )
    def test_errors_raise(self, payload: bytes) -> None:
        with pytest.raises(PayloadError):
            parse_candles(payload, instrument_key=KEY)


def bar(minute: datetime, price: float = 100.0, volume: int = 10) -> Bar:
    return Bar(KEY, minute, price, price + 1, price - 1, price, volume, 0)


class TestBarStore:
    def test_months(self) -> None:
        assert months(date(2025, 11, 20), date(2026, 2, 1)) == [
            date(2025, 11, 1),
            date(2025, 12, 1),
            date(2026, 1, 1),
            date(2026, 2, 1),
        ]

    def test_paths_are_safe_for_index_names(self, tmp_path: Path) -> None:
        path = month_path(tmp_path, "NSE_INDEX|Nifty 50", date(2026, 9, 1))
        assert path == tmp_path / "1m" / "NSE_INDEX" / "Nifty_50" / "2026-09.parquet"
        with pytest.raises(ValueError, match="instrument key"):
            month_path(tmp_path, "RELIANCE", date(2026, 9, 1))

    def test_merge_new_wins_and_nothing_half_written(self, tmp_path: Path) -> None:
        t0 = datetime(2026, 9, 1, 3, 45, tzinfo=UTC)
        sep, minute = date(2026, 9, 1), timedelta(minutes=1)
        assert write_month(tmp_path, KEY, sep, [bar(t0), bar(t0 + minute)]) == 2
        assert write_month(tmp_path, KEY, sep, [bar(t0 + minute, 105), bar(t0 + 2 * minute)]) == 3
        stored = read_month(tmp_path, KEY, date(2026, 9, 1))
        assert [b.open for b in stored] == [100.0, 105.0, 100.0]
        assert not list(tmp_path.rglob("*.tmp"))

    def test_bars_outside_the_month_are_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="outside"):
            write_month(
                tmp_path, KEY, date(2026, 9, 1), [bar(datetime(2026, 10, 1, 3, 45, tzinfo=UTC))]
            )

    def test_reads_across_months_with_duckdb(self, tmp_path: Path) -> None:
        sep30 = datetime(2026, 9, 30, 9, 59, tzinfo=UTC)
        oct1 = datetime(2026, 10, 1, 3, 45, tzinfo=UTC)
        write_month(tmp_path, KEY, date(2026, 9, 1), [bar(sep30)])
        write_month(tmp_path, KEY, date(2026, 10, 1), [bar(oct1), bar(oct1 + timedelta(minutes=1))])
        got = read_bars(tmp_path, KEY, sep30, oct1 + timedelta(minutes=1))
        assert [b.ts for b in got] == [sep30, oct1]
        assert read_bars(tmp_path, "NSE_EQ|INE000000000", sep30, oct1) == []

    def test_summary_and_eod_check(self) -> None:
        t0 = datetime(2026, 10, 1, 3, 45, tzinfo=UTC)
        day = summarize([bar(t0, 100, 600), bar(t0 + timedelta(minutes=1), 103, 400)])
        assert day is not None and (day.open, day.high, day.low, day.volume) == (100, 104, 99, 1000)
        checks = {
            name: ok for name, _, _, ok in eod_check(day, open=100, high=104, low=99, volume=1010)
        }
        assert checks == {"open": True, "high": True, "low": True, "volume": True}
        checks = {
            name: ok for name, _, _, ok in eod_check(day, open=100, high=105, low=99, volume=2000)
        }
        assert checks == {"open": True, "high": False, "low": True, "volume": False}
        assert summarize([]) is None


# --- fetching -----------------------------------------------------------------------


def with_token(svc: Services) -> Services:
    svc.settings.upstox_analytics_token = SecretStr("tok-123")
    return svc


def url_for(svc: Services, month: date, to: date) -> str:
    return candles_url(svc.settings.upstox_api_base, KEY, "minutes", 1, to, month)


async def test_no_token_no_request(svc: Services) -> None:
    with pytest.raises(TokenMissing, match="Analytics Token"):
        await fetch_month(svc, KEY, date(2026, 8, 1))


@respx.mock
async def test_a_past_month_is_fetched_once(svc: Services) -> None:
    with_token(svc)
    route = respx.get(url_for(svc, date(2026, 8, 1), date(2026, 8, 31))).mock(
        return_value=httpx.Response(
            200, content=reply(candle("2026-08-03T09:15:00"), candle("2026-08-03T09:16:00"))
        )
    )
    result = await fetch_month(svc, KEY, date(2026, 8, 1))
    assert (result.status, result.bars) == ("complete", 2)
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer tok-123"
    assert "tok-123" not in str(request.url)
    with svc.engine.begin() as conn:
        row = conn.execute(select(bar_months)).one()
        raw = conn.execute(select(raw_documents.c.kind, raw_documents.c.meta)).all()
    assert (row.status, row.n_bars, row.attempts) == ("complete", 2, 1)
    meta = {"instrument_key": KEY, "from": "2026-08-01", "to": "2026-08-31", "http_status": 200}
    assert ("upstox_candles", meta) in [(kind, stored) for kind, stored in raw]
    assert len(read_month(svc.settings.bars_dir, KEY, date(2026, 8, 1))) == 2

    again = await fetch_month(svc, KEY, date(2026, 8, 1))
    assert again.status == "skipped" and route.call_count == 1  # resumable
    forced = await fetch_month(svc, KEY, date(2026, 8, 1), force=True)
    assert forced.status == "complete" and route.call_count == 2


@respx.mock
async def test_the_current_month_stays_partial(svc: Services) -> None:
    with_token(svc)  # the clock says 2026-09-25
    respx.get(url_for(svc, date(2026, 9, 1), date(2026, 9, 25))).mock(
        return_value=httpx.Response(200, content=reply(candle("2026-09-24T09:15:00")))
    )
    assert (await fetch_month(svc, KEY, date(2026, 9, 1))).status == "partial"


@respx.mock
async def test_a_rejected_request_is_recorded_without_the_token(svc: Services) -> None:
    with_token(svc)
    respx.get(url_for(svc, date(2026, 8, 1), date(2026, 8, 31))).mock(
        return_value=httpx.Response(
            401, json={"status": "error", "errors": [{"message": "Invalid token"}]}
        )
    )
    result = await fetch_month(svc, KEY, date(2026, 8, 1))
    assert result.status == "failed" and result.error and "HTTP 401" in result.error
    with svc.engine.begin() as conn:
        row = conn.execute(select(bar_months)).one()
    assert row.status == "failed" and "tok-123" not in (row.last_error or "")


async def test_months_without_data_are_skipped(svc: Services) -> None:
    with_token(svc)
    assert (await fetch_month(svc, KEY, date(2021, 12, 1))).status == "skipped"
    assert (await fetch_month(svc, KEY, date(2026, 11, 1))).status == "skipped"


def test_key_for_symbol(svc: Services) -> None:
    with svc.engine.begin() as conn:
        assert key_for_symbol(conn, "RELIANCE") is None
        doc = repo.save_raw(
            conn, svc.store, b"EQUITY_L", kind="nse_instruments", source="NSE", url="u",
            content_type=None, fetched_at=datetime(2026, 10, 3, tzinfo=UTC),
        )  # fmt: skip
        conn.execute(
            instrument_snapshots.insert().values(
                as_of_date=date(2026, 10, 3), symbol="RELIANCE", series="EQ",
                isin="INE002A01018", available_at=datetime(2026, 10, 3, tzinfo=UTC),
                raw_doc_id=doc, parser_version="v",
            )
        )  # fmt: skip
        assert key_for_symbol(conn, "reliance") == KEY


def test_cli_candle_probe_explains_what_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GATS_DATA_DIR", str(tmp_path / "data"))
    result = CliRunner().invoke(app, ["probe", "upstox-candles", "--date", "2026-10-01"])
    assert result.exit_code == 1 and "gats probe instruments" in result.output
    shown = CliRunner().invoke(app, ["bars", "show", "--key", KEY, "--date", "2026-10-01"])
    assert shown.exit_code == 1 and "no bars stored" in shown.output
