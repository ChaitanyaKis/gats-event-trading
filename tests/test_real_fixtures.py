"""Parsers against trimmed real payloads (tests/fixtures/real/README.md).

The synthetic builders in conftest.py mirror what we *expect*; these samples
are what the exchanges actually sent. A format change upstream shows up here
first when the fixtures are refreshed.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

import pytest

from gats.sources import bse, nse, nse_archives
from gats.sources.models import AnnouncementRecord, ParseResult
from gats.timeutil import to_ist

REAL = Path(__file__).parent / "fixtures" / "real"
LIVE = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"
ISIN = re.compile(r"^IN[A-Z0-9]{9}[0-9]$")


def load(name: str) -> bytes:
    return (REAL / name).read_bytes()


@pytest.fixture(scope="module")
def bse_parsed() -> ParseResult[AnnouncementRecord]:
    return bse.parse_announcements(load("bse_ann_2026-10-02.json"), attachment_base=LIVE)


@pytest.fixture(scope="module")
def nse_parsed() -> ParseResult[AnnouncementRecord]:
    return nse.parse_announcements(load("nse_ann_2026-10-02.json"))


class TestBseReal:
    @pytest.fixture
    def parsed(
        self, bse_parsed: ParseResult[AnnouncementRecord]
    ) -> ParseResult[AnnouncementRecord]:
        return bse_parsed

    def test_every_row_parses_cleanly(self, parsed: ParseResult[AnnouncementRecord]) -> None:
        assert len(parsed.records) == 30
        assert parsed.warnings == []
        assert parsed.meta["total_pages"] == 6
        assert parsed.meta["row_count"] == 272

    def test_core_fields_are_fully_populated(self, parsed: ParseResult[AnnouncementRecord]) -> None:
        for record in parsed.records:
            assert record.scrip_code and record.scrip_code.isdigit()
            assert record.company_name and record.category and record.subject
            assert record.exch_disseminated_ts is not None
            assert record.exch_submitted_ts is not None
            assert record.event_ts == record.exch_disseminated_ts
            # The probed day, in IST, for every row.
            assert to_ist(record.event_ts).date() == date(2026, 10, 2)
            # BSE disseminates after (or at) submission, never before.
            assert record.exch_submitted_ts <= record.exch_disseminated_ts + timedelta(seconds=1)

    def test_ids_unique_and_newest_first(self, parsed: ParseResult[AnnouncementRecord]) -> None:
        ids = [r.source_ann_id for r in parsed.records]
        assert len(set(ids)) == len(ids)
        stamps = [r.event_ts for r in parsed.records if r.event_ts]
        assert stamps == sorted(stamps, reverse=True)

    def test_attachments_point_at_live_folder(
        self, parsed: ParseResult[AnnouncementRecord]
    ) -> None:
        urls = [r.attachment_url for r in parsed.records if r.attachment_url]
        assert len(urls) >= 25
        # Fld_Attachsize is exact bytes; the first row's PDF is 8,889,958 bytes.
        assert parsed.records[0].attachment_size == 8889958
        assert all(r.attachment_size for r in parsed.records if r.attachment_url)
        assert all(u.startswith(LIVE) and u.lower().endswith(".pdf") for u in urls)

    def test_unmapped_fields_match_data_sources_doc(
        self, parsed: ParseResult[AnnouncementRecord]
    ) -> None:
        # Documented in docs/DATA_SOURCES.md; a new upstream field fails here.
        assert set(parsed.meta["unknown_fields"]) == {
            "agenda_id",
            "announcement_type",
            "audio_video_file",
            "bsenewsid",
            "criticalnews",
            "datainsdate",
            "filestatus",
            "investor_presentation",
            "more",
            "nsurl",
            "old",
            "quarter_id",
            "recordid",
            "rn",
            "timediff",
            "xml_name",
        }


def test_bse_past_day_page_count_comes_from_rowcnt() -> None:
    # Past days have no TotalPageCnt and rename two fields (BSENewsid,
    # Investor_Presentation); the mapping must still be complete.
    parsed = bse.parse_announcements(load("bse_ann_2023-10-03_page1.json"), attachment_base=LIVE)
    assert parsed.warnings == []
    assert parsed.meta["row_count"] == 996
    assert parsed.meta["total_pages"] == 20  # ceil(996 / 50)
    assert "totalpagecnt" not in parsed.meta["unknown_fields"]
    for record in parsed.records:
        assert record.exch_disseminated_ts and record.scrip_code and record.category
        assert to_ist(record.exch_disseminated_ts).date() == date(2023, 10, 3)


class TestNseReal:
    @pytest.fixture
    def parsed(
        self, nse_parsed: ParseResult[AnnouncementRecord]
    ) -> ParseResult[AnnouncementRecord]:
        return nse_parsed

    def test_every_row_parses_cleanly(self, parsed: ParseResult[AnnouncementRecord]) -> None:
        assert len(parsed.records) == 30
        assert parsed.warnings == []

    def test_core_fields_are_fully_populated(self, parsed: ParseResult[AnnouncementRecord]) -> None:
        for record in parsed.records:
            assert record.source_ann_id.isdigit()  # real seq_id, never the hash fallback
            assert record.symbol and record.company_name and record.category
            assert record.isin and ISIN.match(record.isin)
            assert record.exch_disseminated_ts is not None
            assert to_ist(record.exch_disseminated_ts).date() == date(2026, 10, 2)
            # an_dt is the receipt time: at most a few seconds before dissemination.
            assert record.exch_submitted_ts is not None
            lag = record.exch_disseminated_ts - record.exch_submitted_ts
            assert timedelta(0) <= lag <= timedelta(seconds=5)
            assert record.attachment_url and record.attachment_url.startswith(
                "https://nsearchives.nseindia.com/corporate/"
            )

    def test_unmapped_fields_match_data_sources_doc(
        self, parsed: ParseResult[AnnouncementRecord]
    ) -> None:
        assert set(parsed.meta["unknown_fields"]) == {
            "bflag",
            "csvname",
            "hasxbrl",
            "old_new",
            "orgid",
        }

    def test_attachment_sizes(self, parsed: ParseResult[AnnouncementRecord]) -> None:
        # First row: "31.78 KB" -> 31.78 * 1024 bytes.
        assert parsed.records[0].attachment_size == round(31.78 * 1024)
        assert sum(r.attachment_size is not None for r in parsed.records) >= 28

    def test_unique_ids(self, parsed: ParseResult[AnnouncementRecord]) -> None:
        ids = [r.source_ann_id for r in parsed.records]
        assert len(set(ids)) == len(ids)

    def test_subset_payload_round_trips_real_rows(self) -> None:
        payload = load("nse_ann_2026-10-02.json")
        full = nse.parse_announcements(payload).records
        keep = {full[0].source_ann_id, full[5].source_ann_id}
        subset = nse.parse_announcements(nse.subset_payload(payload, keep)).records
        assert {r.source_ann_id for r in subset} == keep
        assert [r for r in full if r.source_ann_id in keep] == subset


class TestNseArchivesReal:
    def test_eod(self) -> None:
        parsed = nse_archives.parse_eod(
            load("nse_eod_2026-10-01.csv"), trade_date=date(2026, 10, 1)
        )
        assert parsed.warnings == []
        assert len(parsed.records) == 30
        series = {r.series for r in parsed.records}
        assert {"EQ", "BE", "SM", "GS"} <= series
        for record in parsed.records:
            assert record.trade_date == date(2026, 10, 1)
            assert None not in (record.open, record.high, record.low, record.close)
            assert record.prev_close is not None and record.volume is not None
            assert record.low <= record.close <= record.high  # type: ignore[operator]
            if record.series == "EQ":
                assert record.deliv_qty is not None and record.deliv_pct is not None
        # Trade-for-trade BE rows carry no delivery split ("-" in the file).
        (be_row,) = [r for r in parsed.records if r.series == "BE"]
        assert be_row.deliv_qty is None and be_row.deliv_pct is None

    def test_eod_rejects_wrong_day(self) -> None:
        parsed = nse_archives.parse_eod(
            load("nse_eod_2026-10-01.csv"), trade_date=date(2026, 10, 2)
        )
        assert parsed.records == []
        assert parsed.warnings == ["30 rows dated 2026-10-01, not 2026-10-02: dropped"]
        assert parsed.meta["dates_seen"] == ["2026-10-01"]

    def test_bands(self) -> None:
        parsed = nse_archives.parse_bands(load("nse_bands_2026-10-02.csv"))
        assert parsed.warnings == []
        assert len(parsed.records) == 30
        assert {r.band for r in parsed.records} == {"2", "5", "10", "20", "40", "No Band"}
        remarks = {r.remarks for r in parsed.records if r.remarks}
        assert "GSM STAGE - 0" in remarks
        # "-" means no remark and is stored as NULL.
        assert all(r.remarks != "-" for r in parsed.records)

    def test_instruments(self) -> None:
        parsed = nse_archives.parse_instruments(load("nse_instruments_2026-10-02.csv"))
        assert parsed.warnings == []
        assert len(parsed.records) == 30
        assert {r.series for r in parsed.records} == {"EQ", "BE", "BZ"}
        for record in parsed.records:
            assert record.isin and ISIN.match(record.isin)
            assert record.name and record.listing_date and record.face_value
            assert record.market_lot == 1
