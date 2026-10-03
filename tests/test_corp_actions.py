"""Corporate actions and split-safe returns (T2.8), on real cases."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import Engine, select

from gats.db import repo
from gats.db.schema import corporate_actions
from gats.ingest import Services
from gats.pit import AsOf
from gats.rawstore import RawStore
from gats.refdata.actions import ReturnAdjuster, store_actions
from gats.refdata.ingest import ingest_nse_corp_actions
from gats.refdata.symbols import store_changes
from gats.sources import nse_archives
from gats.sources.nse_corp_actions import classify, parse_corporate_actions
from gats.sources.nse_symbols import SymbolChangeRecord

REAL = Path(__file__).parent / "fixtures" / "real"
SAMPLE = REAL / "nse_corp_actions_sample.json"
CASES = json.loads((REAL / "ca_eod_cases_2025-09.json").read_text(encoding="utf-8"))
FETCHED = datetime(2026, 10, 3, 6, 0, tzinfo=UTC)
URL = "https://www.nseindia.com/api/corporates-corporateActions"


@pytest.mark.parametrize(
    ("subject", "kind", "multiplier", "review"),
    [
        ("Bonus 1:1", "BONUS", 2.0, False),
        ("Bonus 2:1", "BONUS", 3.0, False),
        ("Bonus- 1:2", "BONUS", 1.5, False),  # AJANTPHARM's wording
        (
            "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 1/- Per Share",
            "SPLIT",
            10.0,
            False,
        ),
        (
            "Face Value Split (Sub-Division) - From Rs 4/- Per Share To Rs 2/- Per Share",
            "SPLIT",
            2.0,
            False,
        ),
        (
            "Consolidation Of Equity Shares From Re 1 Per Share To Rs 10 Per Share",
            "CONSOLIDATION",
            0.1,
            False,
        ),
        ("Rights 1:5 @ Premium Rs 45/-", "RIGHTS", None, True),
        ("Demerger", "DEMERGER", None, True),
        ("Bonus Ncrps 1:116", "DEMERGER", None, True),  # preference shares: value paid out
        (
            "Scheme Of Arangement- Bonus - 1 Debenture For 1 Equity Share Held",
            "DEMERGER",
            None,
            True,
        ),
        ("Dividend - Rs 5 Per Share", "DIVIDEND", None, False),
        ("Buy Back", "BUYBACK", None, False),
        ("Interest Payment", "OTHER", None, False),
    ],
)
def test_classify_real_subjects(
    subject: str, kind: str, multiplier: float | None, review: bool
) -> None:
    got_kind, got_multiplier, _cash, got_review = classify(subject)
    assert (got_kind, got_review) == (kind, review)
    assert got_multiplier == pytest.approx(multiplier) if multiplier else got_multiplier is None


def test_dividend_amounts_add_up() -> None:
    _kind, _m, cash, _r = classify("Dividend - Rs 3 Per Share & Special Dividend - Rs 5 Per Share")
    assert cash == 8.0


def test_parse_real_sample() -> None:
    parsed = parse_corporate_actions(SAMPLE.read_bytes())
    assert parsed.warnings == []
    by = {(r.reported_symbol, r.subject): r for r in parsed.records}
    nazara = [r for r in parsed.records if r.reported_symbol == "NAZARA"]
    assert {r.kind for r in nazara} == {"BONUS", "SPLIT"}
    # LTI's 2019 dividend is reported under today's symbol (LTM).
    assert ("LTM", "Interim Dividend - Rs 12.50 Per Share") in by
    assert by[("LTM", "Interim Dividend - Rs 12.50 Per Share")].ex_date == date(2019, 10, 24)


@pytest.fixture
def adjuster(engine: Engine, store: RawStore) -> ReturnAdjuster:
    parsed = parse_corporate_actions(SAMPLE.read_bytes())
    with engine.begin() as conn:
        doc = repo.save_raw(
            conn,
            store,
            SAMPLE.read_bytes(),
            kind="t",
            source="NSE",
            url="u",
            content_type=None,
            fetched_at=FETCHED,
        )
        store_actions(conn, parsed.records, fetched_at=FETCHED, raw_doc_id=doc, parser_version="v")
        return ReturnAdjuster.load(conn)


def eod_close(line: str, day: str) -> tuple[float | None, float | None]:
    payload = (CASES["header"] + "\n" + line + "\n").encode()
    (record,) = nse_archives.parse_eod(payload, trade_date=date.fromisoformat(day)).records
    return record.prev_close, record.close


@pytest.mark.parametrize("case", CASES["cases"], ids=lambda c: c["symbol"])
def test_real_ex_dates_raw_vs_adjusted(case: dict[str, Any], adjuster: ReturnAdjuster) -> None:
    """The acceptance check: NSE's PREV_CLOSE is the raw previous close, so
    the naive return on an ex-date is a fake crash; the adjusted one is a
    normal daily move."""
    _, prev_close_day_before = eod_close(case["prev_line"], case["prev_day"])
    prev_close, close = eod_close(case["ex_line"], case["ex_day"])
    assert prev_close == prev_close_day_before  # not adjusted by NSE
    ex_day = date.fromisoformat(case["ex_day"])
    naive = close / prev_close - 1  # type: ignore[operator]
    adjusted = adjuster.daily_return(case["symbol"], ex_day, prev_close, close)
    assert naive < -0.3
    # A real daily move. The largest here is ADANIPOWER's +20.0% on its split
    # day (close 170.25 vs 709.40 / 5 = 141.88), against a naive -76%.
    assert adjusted is not None and abs(adjusted) < 0.25
    assert not adjuster.needs_review(case["symbol"], ex_day)


def test_two_actions_on_one_day_multiply(adjuster: ReturnAdjuster) -> None:
    day = adjuster.on("NAZARA", date(2025, 9, 26))
    assert day is not None and day.multiplier == pytest.approx(4.0)
    assert len(day.subjects) == 2


def test_symbol_reported_today_maps_to_the_ex_date_symbol(engine: Engine, store: RawStore) -> None:
    parsed = parse_corporate_actions(SAMPLE.read_bytes())
    with engine.begin() as conn:
        doc = repo.save_raw(
            conn,
            store,
            b"x",
            kind="t",
            source="NSE",
            url="u",
            content_type=None,
            fetched_at=FETCHED,
        )
        store_changes(
            conn,
            [
                SymbolChangeRecord("LTI", "LTIM", date(2022, 12, 5), None),
                SymbolChangeRecord("LTIM", "LTM", date(2026, 2, 27), None),
            ],
            fetched_at=FETCHED,
            raw_doc_id=doc,
            parser_version="v",
        )
        store_actions(conn, parsed.records, fetched_at=FETCHED, raw_doc_id=doc, parser_version="v")
        adjuster = ReturnAdjuster.load(conn)
    found = adjuster.on("LTI", date(2019, 10, 24))
    assert found is not None and "Interim Dividend - Rs 12.50 Per Share" in found.subjects
    assert adjuster.on("LTM", date(2019, 10, 24)) is None


def test_review_days_are_flagged(adjuster: ReturnAdjuster) -> None:
    parsed = parse_corporate_actions(SAMPLE.read_bytes())
    demerger = next(r for r in parsed.records if r.kind == "DEMERGER" and r.subject == "Demerger")
    assert adjuster.needs_review(demerger.reported_symbol, demerger.ex_date)


def test_point_in_time(engine: Engine, store: RawStore) -> None:
    parsed = parse_corporate_actions(SAMPLE.read_bytes())
    with engine.begin() as conn:
        doc = repo.save_raw(
            conn,
            store,
            b"x",
            kind="t",
            source="NSE",
            url="u",
            content_type=None,
            fetched_at=FETCHED,
        )
        store_actions(conn, parsed.records, fetched_at=FETCHED, raw_doc_id=doc, parser_version="v")
        # Backfilled actions become knowable at 00:00 IST on their ex-date.
        rows = {
            r.reported_symbol + r.subject: r.available_at
            for r in conn.execute(select(corporate_actions))
        }
        assert rows["PIDILITINDBonus 1:1"] == datetime(2025, 9, 22, 18, 30, tzinfo=UTC)
        before = AsOf(conn, datetime(2025, 9, 22, 18, 0, tzinfo=UTC)).return_adjuster()
        after = AsOf(conn, datetime(2025, 9, 22, 18, 30, tzinfo=UTC)).return_adjuster()
    assert before.multiplier("PIDILITIND", date(2025, 9, 23)) == 1.0
    assert after.multiplier("PIDILITIND", date(2025, 9, 23)) == 2.0


def test_store_is_idempotent(engine: Engine, store: RawStore) -> None:
    parsed = parse_corporate_actions(SAMPLE.read_bytes())
    with engine.begin() as conn:
        doc = repo.save_raw(
            conn,
            store,
            b"x",
            kind="t",
            source="NSE",
            url="u",
            content_type=None,
            fetched_at=FETCHED,
        )
        first = store_actions(
            conn, parsed.records, fetched_at=FETCHED, raw_doc_id=doc, parser_version="v"
        )
        again = store_actions(
            conn, parsed.records, fetched_at=FETCHED, raw_doc_id=doc, parser_version="v"
        )
    assert first == len(parsed.records) and again == 0


@respx.mock
async def test_ingest_window(svc: Services) -> None:
    respx.get(svc.settings.nse_home_url).mock(return_value=httpx.Response(200))
    route = respx.get(URL).mock(return_value=httpx.Response(200, content=SAMPLE.read_bytes()))
    outcome = await ingest_nse_corp_actions(svc, date(2025, 9, 1), date(2025, 9, 30), job="t")
    assert outcome.ok and outcome.n_new == len(parse_corporate_actions(SAMPLE.read_bytes()).records)
    params = route.calls[0].request.url.params
    assert (params["index"], params["from_date"], params["to_date"]) == (
        "equities",
        "01-09-2025",
        "30-09-2025",
    )
