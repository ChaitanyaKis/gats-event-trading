"""NSE financial results parsers (T4.7), on real payloads probed 2026-10-03."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from gats.sources.models import PayloadError
from gats.sources.nse_results import (
    integrated_params,
    legacy_params,
    parse_integrated_index,
    parse_legacy_index,
    parse_results_xbrl,
)

REAL = Path(__file__).parent / "fixtures" / "real"
LEGACY = (REAL / "nse_financial_results_RELIANCE_2026-10-03.json").read_bytes()
INTEGRATED = (REAL / "nse_integrated_filing_RELIANCE_2026-10-03.json").read_bytes()


def test_legacy_index_lists_quarters_with_dissemination_times() -> None:
    result = parse_legacy_index(LEGACY)
    assert result.meta == {"rows": 8} and len(result.records) == 8 and not result.warnings
    newest = result.records[0]
    assert (newest.symbol, newest.isin, newest.regime) == ("RELIANCE", "INE002A01018", "legacy")
    assert (newest.period_start, newest.period_end) == (date(2024, 10, 1), date(2024, 12, 31))
    assert (newest.consolidated, newest.audited) == (False, False)
    assert newest.disseminated_ts == datetime(2025, 1, 16, 14, 50, 54, tzinfo=UTC)  # 20:20:54 IST
    assert newest.xbrl_url is not None and newest.xbrl_url.endswith("16012025082021.xml")
    assert {r.consolidated for r in result.records[:6]} == {True, False}
    oldest = result.records[-1]
    assert oldest.period_end == date(2005, 3, 31) and oldest.audited
    assert oldest.disseminated_ts is None and oldest.xbrl_url is None  # "xbrl/-" is no file


def test_integrated_index_continues_where_the_legacy_one_stops() -> None:
    result = parse_integrated_index(INTEGRATED)
    assert result.meta == {"rows": 12, "total": 12}
    ends = sorted({r.period_end for r in result.records})
    assert ends[0] == date(2025, 3, 31) and ends[-1] == date(2026, 6, 30)
    newest = result.records[0]
    assert newest.consolidated and not newest.audited and not newest.revised
    # broadcast 19:50:03 IST, created 19:50:04 IST: the later clock is used
    assert newest.disseminated_ts == datetime(2026, 7, 17, 14, 20, 4, tzinfo=UTC)
    assert newest.xbrl_url is not None and "INTEGRATED_FILING_INDAS" in newest.xbrl_url
    assert sum(r.audited for r in result.records) == 4  # the two March quarters, both variants


def test_revenue_from_both_xbrl_taxonomies() -> None:
    legacy = parse_results_xbrl(
        (REAL / "nse_xbrl_results_RELIANCE_2024-12-31_consolidated.xml").read_bytes()
    )
    assert legacy.revenue == 2_438_650_000_000.0  # the quarter, not the 9-month figure in FourD
    assert (legacy.period_start, legacy.period_end) == (date(2024, 10, 1), date(2024, 12, 31))
    assert legacy.consolidated is True and legacy.symbol == "RELIANCE"
    new = parse_results_xbrl(
        (REAL / "nse_xbrl_integrated_RELIANCE_2026-06-30_consolidated.xml").read_bytes()
    )
    assert new.revenue == 3_118_500_000_000.0
    assert (new.period_start, new.period_end) == (date(2026, 4, 1), date(2026, 6, 30))
    assert new.consolidated is True and new.isin == "INE002A01018"


def test_request_parameters() -> None:
    assert legacy_params("RELIANCE") == {
        "index": "equities",
        "symbol": "RELIANCE",
        "period": "Quarterly",
    }
    assert integrated_params("RELIANCE")["type"] == "Integrated Filing- Financials"


@pytest.mark.parametrize(
    ("parser", "payload"),
    [
        (parse_legacy_index, b"<html>Access Denied</html>"),
        (parse_legacy_index, b'{"data": []}'),
        (parse_integrated_index, b"[]"),
        (parse_results_xbrl, b"not xml at all"),
    ],
)
def test_wrong_payloads_raise(parser: object, payload: bytes) -> None:
    with pytest.raises(PayloadError):
        parser(payload)  # type: ignore[operator]


def test_a_statement_without_revenue_has_none() -> None:
    # Banks report interest earned, not revenue from operations (synthetic sample).
    bank = (
        b'<x:xbrl xmlns:x="http://www.xbrl.org/2003/instance" xmlns:f="urn:f">'
        b'<f:InterestEarned contextRef="OneD">5</f:InterestEarned></x:xbrl>'
    )
    assert parse_results_xbrl(bank).revenue is None
