"""NSE served an Excel workbook under a ``.csv`` address (verified 2026-10-04:
``sec_bhavdata_full_08082022.csv``). It stopped a seven-year backfill with a
raw ``csv.Error``. The parser now reads the workbook, and no unreadable file
can crash an ingestion: it becomes a recorded bad payload.

The fixture is that real file trimmed to seven data rows (same zip parts).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import func, select

from gats import ingest
from gats.db.schema import eod_prices, raw_documents
from gats.ingest import Services
from gats.sources._util import _column, is_workbook, read_csv, workbook_rows
from gats.sources.models import PayloadError
from gats.sources.nse_archives import EOD_PARSER_VERSION, parse_eod
from tests.conftest import NSE_HOME

WORKBOOK = (
    Path(__file__).parent / "fixtures" / "real" / "nse_eod_08082022_workbook_trimmed.csv"
).read_bytes()
EOD_URL = "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{}.csv"
DAY = date(2022, 8, 8)


def test_the_workbook_is_read_like_the_csv_it_replaced() -> None:
    assert is_workbook(WORKBOOK) and not is_workbook(b"SYMBOL, SERIES\nA, EQ\n")
    rows = workbook_rows(WORKBOOK)
    assert [cell.strip() for cell in rows[0][:4]] == ["SYMBOL", "SERIES", "DATE1", "PREV_CLOSE"]
    assert len(rows) == 8 and all(len(row) == 15 for row in rows)

    parsed = parse_eod(WORKBOOK, trade_date=DAY)
    assert parsed.warnings == [] and parsed.meta["dates_seen"] == ["2022-08-08"]
    assert len(parsed.records) == 7 and EOD_PARSER_VERSION == "nse-eod-v2"
    first = parsed.records[0]
    assert (first.symbol, first.series, first.trade_date) == ("20MICRONS", "EQ", DAY)
    assert (first.open, first.high, first.low, first.close) == (109.45, 115.45, 108.7, 111.15)
    (reliance,) = [r for r in parsed.records if r.symbol == "RELIANCE"]
    assert (reliance.prev_close, reliance.close, reliance.volume) == (2534.0, 2567.15, 4691228)
    assert (reliance.turnover_lacs, reliance.num_trades is not None) == (120444.18, True)
    assert reliance.deliv_pct == 55.76  # written as 55.76 with float noise in some cells


def test_cell_references_and_stray_line_breaks() -> None:
    assert [_column(ref) for ref in ("A1", "O2256", "Z9", "AA1", "AB7")] == [0, 14, 25, 26, 27]
    with pytest.raises(PayloadError, match="bad cell reference"):
        _column("12")
    header, rows = read_csv(b"A, B\r\n1,2\r3,4\n\n")  # a bare carriage return is a line break
    assert header == ["A", "B"] and rows == [{"A": "1", "B": "2"}, {"A": "3", "B": "4"}]


def test_an_unreadable_file_is_a_payload_error_never_a_crash() -> None:
    with pytest.raises(PayloadError, match="workbook: cannot be read"):
        read_csv(b"PK\x03\x04 this only looks like a zip")
    oversized = b'A,B\n"' + b"x" * 200_000 + b'",1\n'  # one field beyond the csv module's limit
    with pytest.raises(PayloadError, match="not readable as CSV"):
        read_csv(oversized)


@respx.mock
async def test_ingestion_takes_the_workbook_and_survives_a_broken_one(svc: Services) -> None:
    respx.get(NSE_HOME).mock(return_value=httpx.Response(200))
    respx.get(EOD_URL.format("08082022")).mock(return_value=httpx.Response(200, content=WORKBOOK))
    respx.get(EOD_URL.format("09082022")).mock(
        return_value=httpx.Response(200, content=b"PK\x03\x04 broken")
    )
    good = await ingest.ingest_eod_day(svc, DAY, job="t", mode="backfill")
    assert good.ok and good.n_records == 7
    bad = await ingest.ingest_eod_day(svc, date(2022, 8, 9), job="t", mode="backfill")
    assert not bad.ok and "workbook" in (bad.error or "")  # reported; the backfill goes on
    with svc.engine.begin() as conn:
        assert conn.execute(select(func.count()).select_from(eod_prices)).scalar_one() == 7
        versions = set(conn.execute(select(eod_prices.c.parser_version)).scalars())
        stored = conn.execute(select(func.count()).select_from(raw_documents)).scalar_one()
    assert versions == {"nse-eod-v2"}
    assert stored >= 2  # raw first: both payloads are kept, the broken one too
