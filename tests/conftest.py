"""Shared fixtures.

Exchange payload builders below are SYNTHETIC: they make it easy to build
edge cases (throttled pages, missing ids). Trimmed real payloads live in
tests/fixtures/real/ and are tested in test_real_fixtures.py.
"""

from __future__ import annotations

import json
import shutil
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

REPO = Path(__file__).parents[1]
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
def command_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A working directory for running a research command: no ``.env``, the
    data directory of the ``settings`` fixture, and the cost files that the
    registered configs name by a path relative to where the command runs."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GATS_DATA_DIR", str(tmp_path / "data"))
    shutil.copytree(REPO / "configs" / "costs", tmp_path / "configs" / "costs")
    return tmp_path


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


# --- typed filings with text (M4 extraction tests) -------------------------------

TAXONOMY_TEST = "taxonomy-test"
SEEDED_AT = datetime(2026, 10, 3, tzinfo=UTC)


def seed_filings(engine: Engine, store: RawStore, filings: list[dict[str, Any]]) -> list[int]:
    """Typed filings whose attachments are downloaded and read. Each dict
    needs ``text``; optional: ``source`` (NSE), ``event_ts``, ``pdf`` (equal
    bytes = one shared document), ``event_type`` (ORDER_WIN), ``symbol``,
    ``details``, ``needs_ocr``. Returns the announcement ids, in order."""
    from sqlalchemy import func, select

    from gats.db import repo
    from gats.db.schema import announcement_event_types, announcements, document_texts
    from gats.extract.pdf_text import EXTRACTOR, EXTRACTOR_VERSION
    from gats.sources.models import AnnouncementRecord

    ids = []
    with engine.begin() as conn:
        first = int(conn.execute(select(func.count()).select_from(announcements)).scalar_one())
        page = repo.save_raw(
            conn, store, b"page", kind="t", source="NSE", url="u", content_type=None,
            fetched_at=SEEDED_AT,
        )  # fmt: skip
        for n, spec in enumerate(filings, start=first):
            event_ts = spec.get("event_ts", SEEDED_AT)
            record = AnnouncementRecord(
                source=spec.get("source", "NSE"),
                source_ann_id=str(n),
                symbol=spec.get("symbol", f"CO{n}"),
                scrip_code=None,
                isin=None,
                company_name=f"Company {n}",
                category="Bagging/Receiving of orders/contracts",
                subcategory=None,
                subject=None,
                details=spec.get("details"),
                attachment_url=f"https://example.com/{n}.pdf",
                exch_submitted_ts=None,
                exch_disseminated_ts=event_ts,
                event_ts=event_ts,
            )
            repo.insert_announcements(
                conn, [record], raw_doc_id=page, parser_version="v", mode="backfill",
                fetched_at=SEEDED_AT, now=SEEDED_AT,
            )  # fmt: skip
            ann_id = int(
                conn.execute(
                    select(announcements.c.id).where(
                        announcements.c.source == record.source,
                        announcements.c.source_ann_id == record.source_ann_id,
                    )
                ).scalar_one()
            )
            doc = repo.save_raw(
                conn, store, spec.get("pdf", f"pdf {n}".encode()), kind="attachment",
                source=record.source, url=record.attachment_url or "",
                content_type="application/pdf", fetched_at=SEEDED_AT,
            )  # fmt: skip
            repo.mark_attachment(conn, ann_id, status="done", doc_id=doc)
            conn.execute(
                announcement_event_types.insert().values(
                    announcement_id=ann_id,
                    taxonomy_version=TAXONOMY_TEST,
                    event_type=spec.get("event_type", "ORDER_WIN"),
                    rule_no=0,
                    classified_at=SEEDED_AT,
                )
            )
            repo.insert_ignore(
                conn,
                document_texts,
                [
                    {
                        "doc_id": doc,
                        "extractor": EXTRACTOR,
                        "extractor_version": EXTRACTOR_VERSION,
                        "pages": 1,
                        "chars": len(spec["text"]),
                        "needs_ocr": spec.get("needs_ocr", False),
                        "error": None,
                        "text": spec["text"],
                        "extracted_at": SEEDED_AT,
                    }
                ],
                ["doc_id", "extractor", "extractor_version"],
            )
            ids.append(ann_id)
    return ids


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


def bse_payload(
    rows: list[dict[str, Any]], total_pages: int | None = 1, row_count: int | None = None
) -> bytes:
    """A BSE page. ``total_pages=None`` mimics past days (no TotalPageCnt);
    ``row_count`` is the day's total (Table1.ROWCNT), omitted when None."""
    for row in rows:
        if total_pages is None:
            row.pop("TotalPageCnt", None)
        else:
            row["TotalPageCnt"] = total_pages
    data: dict[str, Any] = {"Table": rows}
    if row_count is not None:
        data["Table1"] = [{"ROWCNT": row_count}]
    return json.dumps(data).encode()


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
