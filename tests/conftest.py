"""Shared fixtures.

Exchange payload builders below are SYNTHETIC: they make it easy to build
edge cases (throttled pages, missing ids). Trimmed real payloads live in
tests/fixtures/real/ and are tested in test_real_fixtures.py.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine

from gats.config import Settings
from gats.db.engine import init_db, make_engine
from gats.ingest import Services
from gats.net import PoliteClient
from gats.rawstore import RawStore

BSE_URL = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
NSE_URL = "https://www.nseindia.com/api/corporate-announcements"
NSE_HOME = "https://www.nseindia.com/"


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class SleepRecorder:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings(
        _env_file=None,  # type: ignore[call-arg]
        data_dir=tmp_path / "data",
        min_request_interval_s=0,
        max_retries=2,
        backoff_base_s=0.001,
        backoff_max_s=0.01,
        bse_warmup=False,  # tested explicitly in test_ingest
        bse_page_retry_delay_s=5.0,
        host_min_interval_s={},
    )
    s.ensure_dirs()
    return s


@pytest.fixture
def engine(settings: Settings) -> Iterator[Engine]:
    eng = make_engine(settings.resolved_db_url)
    init_db(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def store(settings: Settings) -> RawStore:
    return RawStore(settings.raw_dir)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 9, 25, 6, 0, tzinfo=UTC))  # 11:30 IST, a Friday


@pytest.fixture
def sleeper() -> SleepRecorder:
    return SleepRecorder()


@pytest.fixture
async def svc(
    settings: Settings, engine: Engine, store: RawStore, clock: FakeClock, sleeper: SleepRecorder
) -> AsyncIterator[Services]:
    client = PoliteClient(
        user_agent="test",
        min_interval_s=0,
        max_retries=settings.max_retries,
        backoff_base_s=settings.backoff_base_s,
        backoff_max_s=settings.backoff_max_s,
        sleep=sleeper,
    )
    yield Services(
        settings=settings, engine=engine, store=store, client=client, clock=clock, sleep=sleeper
    )
    await client.aclose()


# --- payload builders ------------------------------------------------------------


def bse_row(news_id: str, dissem: str = "2026-09-25T10:15:30.123", **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "NEWSID": news_id,
        "SCRIP_CD": 500325,
        "SLONGNAME": "Example Industries Ltd",
        "CATEGORYNAME": "Company Update",
        "SUBCATNAME": "Award of Order / Receipt of Order",
        "NEWSSUB": "Example Industries Ltd - 500325 - Receipt of order",
        "HEADLINE": "Company has received an order worth Rs 150 crore",
        "ATTACHMENTNAME": f"{news_id}.pdf",
        "PDFFLAG": 0,
        "NEWS_DT": dissem,
        "DissemDT": dissem,
        "News_submission_dt": "2026-09-25T10:14:02",
        "TotalPageCnt": 1,
    }
    row.update(extra)
    return row


def bse_payload(rows: list[dict[str, Any]], total_pages: int = 1) -> bytes:
    for row in rows:
        row["TotalPageCnt"] = total_pages
    return json.dumps({"Table": rows, "Table1": [{"ROWCNT": len(rows) * total_pages}]}).encode()


def nse_row(seq_id: str | None, **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "symbol": "EXAMPLE",
        "desc": "Receipt of Order",
        "sm_name": "Example Industries Limited",
        "sm_isin": "INE000A01010",
        "an_dt": "25-Sep-2026 10:15:30",
        "exchdisstime": "25-Sep-2026 10:15:31",
        "attchmntFile": "https://nsearchives.nseindia.com/corporate/EXAMPLE_25092026.pdf",
        "attchmntText": "Received order worth Rs 150 crore",
        "smIndustry": "Capital Goods",
    }
    if seq_id is not None:
        row["seq_id"] = seq_id
    row.update(extra)
    return row


EOD_CSV = (
    b"SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, "
    b"CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER\n"
    b"EXAMPLE, EQ, 25-Sep-2026, 100.00, 101.00, 110.50, 99.50, 108.00, 108.20, 105.37, "
    b"1234567, 1300.55, 8901, 456789, 37.00\n"
    b"EXAMPLE, BL, 25-Sep-2026, 100.00, 102.00, 102.00, 102.00, 102.00, 102.00, 102.00, "
    b"5000, 5.10, 1, -, -\n"
)

BANDS_CSV = (
    b"Symbol,Series,Security Name,Band,Remarks\n"
    b"EXAMPLE,EQ,Example Industries Limited,20,\n"
    b"TINYCO,BE,Tiny Co Limited,5,-\n"
)

INSTRUMENTS_CSV = (
    b"SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, "
    b"ISIN NUMBER, FACE VALUE\n"
    b"EXAMPLE,Example Industries Limited,EQ,06-OCT-2008,10,1,INE000A01010,10\n"
)


JsonFactory = Callable[..., bytes]
