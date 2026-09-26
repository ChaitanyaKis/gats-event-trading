from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from gats.sources import bse, nse, nse_archives
from gats.sources.models import PayloadError
from tests.conftest import (
    BANDS_CSV,
    EOD_CSV,
    INSTRUMENTS_CSV,
    bse_payload,
    bse_row,
    nse_row,
)

LIVE = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"
HIST = "https://www.bseindia.com/xml-data/corpfiling/AttachHis/"


class TestBse:
    def test_maps_fields_and_times(self) -> None:
        result = bse.parse_announcements(
            bse_payload([bse_row("abc-1")], total_pages=3), attachment_base=LIVE
        )
        assert result.warnings == []
        assert result.meta["total_pages"] == 3
        (record,) = result.records
        assert record.source == "BSE"
        assert record.source_ann_id == "abc-1"
        assert record.scrip_code == "500325"
        assert record.subcategory == "Award of Order / Receipt of Order"
        assert record.attachment_url == LIVE + "abc-1.pdf"
        assert record.exch_disseminated_ts == datetime(2026, 9, 25, 4, 45, 30, 123000, tzinfo=UTC)
        assert record.exch_submitted_ts == datetime(2026, 9, 25, 4, 44, 2, tzinfo=UTC)
        assert record.event_ts == record.exch_disseminated_ts

    def test_falls_back_to_news_dt_and_reports_unknown_fields(self) -> None:
        row = bse_row("abc-2", NEW_FIELD="x")
        del row["DissemDT"]
        result = bse.parse_announcements(bse_payload([row]), attachment_base=LIVE)
        assert result.records[0].exch_disseminated_ts is None
        assert result.records[0].event_ts == datetime(2026, 9, 25, 4, 45, 30, 123000, tzinfo=UTC)
        assert result.meta["unknown_fields"] == ["new_field"]

    def test_skips_rows_without_id(self) -> None:
        result = bse.parse_announcements(
            bse_payload([bse_row("", ATTACHMENTNAME=""), bse_row("ok")]), attachment_base=LIVE
        )
        assert [r.source_ann_id for r in result.records] == ["ok"]
        assert "missing NEWSID" in result.warnings[0]

    def test_html_block_page_is_payload_error(self) -> None:
        with pytest.raises(PayloadError, match="HTML"):
            bse.parse_announcements(
                b"<!DOCTYPE html><html>Access Denied</html>", attachment_base=LIVE
            )

    def test_wrong_shape_is_payload_error(self) -> None:
        with pytest.raises(PayloadError):
            bse.parse_announcements(json.dumps({"x": 1}).encode(), attachment_base=LIVE)

    def test_attachment_candidates_try_other_folder(self) -> None:
        assert bse.attachment_candidates(LIVE + "a.pdf", LIVE, HIST) == [
            LIVE + "a.pdf",
            HIST + "a.pdf",
        ]
        assert bse.attachment_candidates("https://x/a.pdf", LIVE, HIST) == ["https://x/a.pdf"]

    def test_request_params(self) -> None:
        params = bse.request_params(date(2026, 9, 24), date(2026, 9, 25), 2)
        assert params["strPrevDate"] == "20260924"
        assert params["strToDate"] == "20260925"
        assert params["pageno"] == "2"


class TestNse:
    def test_list_payload(self) -> None:
        result = nse.parse_announcements(json.dumps([nse_row("111")]).encode())
        (record,) = result.records
        assert record.source_ann_id == "111"
        assert record.symbol == "EXAMPLE"
        assert record.isin == "INE000A01010"
        assert record.exch_disseminated_ts == datetime(2026, 9, 25, 4, 45, 31, tzinfo=UTC)
        assert record.event_ts == record.exch_disseminated_ts

    def test_data_wrapper_and_fallback_id(self) -> None:
        payload = json.dumps({"data": [nse_row(None)]}).encode()
        first = nse.parse_announcements(payload)
        second = nse.parse_announcements(payload)
        assert first.records[0].source_ann_id.startswith("h:")
        # Deterministic, so re-polling does not create duplicates.
        assert first.records[0].source_ann_id == second.records[0].source_ann_id
        assert "content hash" in first.warnings[0]

    def test_html_is_payload_error(self) -> None:
        with pytest.raises(PayloadError, match="HTML"):
            nse.parse_announcements(b"<html><body>Resource not found</body></html>")


class TestNseArchives:
    def test_eod_strips_headers_and_handles_dashes(self) -> None:
        result = nse_archives.parse_eod(EOD_CSV, trade_date=date(2026, 9, 25))
        eq, bl = result.records
        assert (eq.symbol, eq.series, eq.close, eq.volume) == ("EXAMPLE", "EQ", 108.2, 1234567)
        assert eq.deliv_qty == 456789
        assert eq.deliv_pct == 37.0
        assert bl.deliv_qty is None and bl.deliv_pct is None

    def test_eod_drops_rows_from_other_dates(self) -> None:
        result = nse_archives.parse_eod(EOD_CSV, trade_date=date(2026, 9, 24))
        assert result.records == []
        assert len(result.warnings) == 2

    def test_eod_missing_columns_fails_loudly(self) -> None:
        with pytest.raises(PayloadError, match="missing columns"):
            nse_archives.parse_eod(b"A,B\n1,2\n", trade_date=date(2026, 9, 25))

    def test_eod_url(self) -> None:
        assert nse_archives.eod_url("x_{ddmmyyyy}.csv", date(2026, 9, 5)) == "x_05092026.csv"

    def test_bands(self) -> None:
        result = nse_archives.parse_bands(BANDS_CSV)
        assert [(r.symbol, r.series, r.band, r.remarks) for r in result.records] == [
            ("EXAMPLE", "EQ", "20", None),
            ("TINYCO", "BE", "5", None),
        ]

    def test_instruments(self) -> None:
        (record,) = nse_archives.parse_instruments(INSTRUMENTS_CSV).records
        assert record.isin == "INE000A01010"
        assert record.listing_date == date(2008, 10, 6)
        assert record.market_lot == 1

    def test_utf8_bom_is_handled(self) -> None:
        result = nse_archives.parse_bands(b"\xef\xbb\xbf" + BANDS_CSV)
        assert len(result.records) == 2


def test_bse_empty_object_means_no_data() -> None:
    result = bse.parse_announcements(b"{}", attachment_base=LIVE)
    assert result.records == []
    assert result.meta["empty_object"] is True
    assert "returned {}" in result.warnings[0]
