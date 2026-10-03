"""Trading calendar (T2.6): weekends, holidays, special sessions, midnight."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time

import httpx
import pytest
import respx
from sqlalchemy import Engine

from gats.db import repo
from gats.db.schema import index_days
from gats.ingest import Services
from gats.pit import AsOf
from gats.rawstore import RawStore
from gats.refdata.calendar import CalendarError, TradingCalendar
from gats.refdata.ingest import ingest_nse_holidays
from gats.sources import nse_holidays
from gats.sources.models import EodRecord, PayloadError
from gats.timeutil import IST

HOLIDAYS_URL = "https://www.nseindia.com/api/holiday-master"


def ist(y: int, m: int, d: int, hh: int = 0, mm: int = 0, ss: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, ss, tzinfo=IST)


@pytest.fixture
def cal() -> TradingCalendar:
    # Real 2026 dates: Thu 1 Oct session, Fri 2 Oct Gandhi Jayanti (the EOD
    # URL served 1 Oct's file), Sun 1 Feb budget-day special session,
    # Tue 20 Oct Dussehra (future holiday, from NSE's list only).
    return TradingCalendar(
        sessions=[date(2026, 10, 1), date(2026, 2, 1), date(2026, 1, 30), date(2026, 2, 2)],
        closed=[date(2026, 10, 2), date(2026, 1, 31)],
        holidays={date(2026, 10, 2): "Mahatma Gandhi Jayanti", date(2026, 10, 20): "Dussehra"},
    )


class TestDays:
    def test_weekends_and_holidays(self, cal: TradingCalendar) -> None:
        assert cal.is_trading_day(date(2026, 10, 1))
        assert not cal.is_trading_day(date(2026, 10, 2))  # holiday
        assert not cal.is_trading_day(date(2026, 10, 3))  # Saturday
        assert not cal.is_trading_day(date(2026, 10, 4))  # Sunday
        assert cal.is_trading_day(date(2026, 10, 5))  # future weekday: assumed open
        assert not cal.is_trading_day(date(2026, 10, 20))  # listed future holiday
        assert cal.holiday_name(date(2026, 10, 20)) == "Dussehra"

    def test_special_session_on_a_sunday(self, cal: TradingCalendar) -> None:
        sunday = date(2026, 2, 1)
        assert cal.is_trading_day(sunday) and cal.is_special_session(sunday)
        assert not cal.is_regular_session(sunday)
        assert cal.trading_days(date(2026, 1, 30), date(2026, 2, 2)) == [
            date(2026, 1, 30),
            sunday,
            date(2026, 2, 2),
        ]
        assert cal.trading_days(date(2026, 1, 30), date(2026, 2, 2), include_special=False) == [
            date(2026, 1, 30),
            date(2026, 2, 2),
        ]
        with pytest.raises(CalendarError, match="special session"):
            cal.session_open(sunday)

    def test_eod_evidence_beats_the_holiday_list(self) -> None:
        # Diwali Laxmi Pujan 2025 was a listed holiday with a Muhurat session.
        muhurat = date(2025, 10, 21)
        cal = TradingCalendar(sessions=[muhurat], holidays={muhurat: "Diwali Laxmi Pujan*"})
        assert cal.is_trading_day(muhurat) and cal.is_special_session(muhurat)

    def test_shift_counts_regular_sessions(self, cal: TradingCalendar) -> None:
        assert cal.shift(date(2026, 10, 1), 1) == date(2026, 10, 5)  # skips holiday + weekend
        assert cal.shift(date(2026, 10, 5), -1) == date(2026, 10, 1)
        assert cal.shift(date(2026, 1, 30), 1) == date(2026, 2, 2)  # skips special Sunday
        assert cal.shift(date(2026, 10, 1), 0) == date(2026, 10, 1)
        with pytest.raises(CalendarError):
            cal.shift(date(2026, 10, 2), 0)


class TestTimes:
    def test_session_times_are_ist(self, cal: TradingCalendar) -> None:
        assert cal.session_open(date(2026, 10, 1)) == datetime(2026, 10, 1, 3, 45, tzinfo=UTC)
        assert cal.session_close(date(2026, 10, 1)) == datetime(2026, 10, 1, 10, 0, tzinfo=UTC)

    @pytest.mark.parametrize(
        ("after", "expected"),
        [
            (ist(2026, 10, 1, 9, 14, 59), ist(2026, 10, 1, 9, 15)),  # before the open
            (ist(2026, 10, 1, 9, 15), ist(2026, 10, 5, 9, 15)),  # exactly at it: strictly after
            (ist(2026, 10, 1, 15, 31), ist(2026, 10, 5, 9, 15)),  # Thu close -> Mon
            (ist(2026, 10, 2, 11, 0), ist(2026, 10, 5, 9, 15)),  # during a holiday
            (ist(2026, 10, 4, 23, 59, 59), ist(2026, 10, 5, 9, 15)),  # Sunday 1 s to midnight
            (ist(2026, 10, 5, 0, 0, 1), ist(2026, 10, 5, 9, 15)),  # Monday just after midnight
            (ist(2026, 1, 31, 12, 0), ist(2026, 2, 2, 9, 15)),  # Sat: special Sunday skipped
        ],
    )
    def test_next_session_open(
        self, cal: TradingCalendar, after: datetime, expected: datetime
    ) -> None:
        assert cal.next_session_open(after) == expected

    def test_ist_date_not_utc_date_decides_the_day(self, cal: TradingCalendar) -> None:
        # 2026-10-04 19:00 UTC is already Monday 00:30 IST.
        moment = datetime(2026, 10, 4, 19, 0, tzinfo=UTC)
        assert cal.next_session_open(moment) == ist(2026, 10, 5, 9, 15)
        # 2026-10-01 03:44 UTC is 09:14 IST: same-day open, not tomorrow's.
        assert cal.next_session_open(datetime(2026, 10, 1, 3, 44, tzinfo=UTC)) == ist(
            2026, 10, 1, 9, 15
        )

    def test_session_of(self, cal: TradingCalendar) -> None:
        assert cal.session_of(ist(2026, 10, 1, 9, 15)) == date(2026, 10, 1)
        assert cal.session_of(ist(2026, 10, 1, 15, 29, 59)) == date(2026, 10, 1)
        assert cal.session_of(ist(2026, 10, 1, 15, 30)) is None
        assert cal.session_of(ist(2026, 10, 2, 11, 0)) is None

    def test_naive_times_are_rejected(self, cal: TradingCalendar) -> None:
        with pytest.raises(ValueError, match="naive"):
            cal.next_session_open(datetime(2026, 10, 1, 9, 0))


class TestLoad:
    def test_from_eod_bookkeeping_and_holiday_list(self, engine: Engine, store: RawStore) -> None:
        now = datetime(2026, 10, 3, 6, 0, tzinfo=UTC)
        with engine.begin() as conn:
            doc = repo.save_raw(
                conn,
                store,
                b"x",
                kind="t",
                source="NSE",
                url="u",
                content_type=None,
                fetched_at=now,
            )
            repo.upsert_eod(
                conn,
                [EodRecord(date(2026, 10, 1), "X", "EQ", *([1.0] * 7), 1, 1.0, 1, 1, 1.0)],
                raw_doc_id=doc,
                parser_version="v",
                available_at=now,
            )
            repo.record_eod_day(
                conn,
                date(2026, 9, 14),
                status="other_day",
                n_records=0,
                now=now,
                file_date=date(2026, 9, 11),
                http_status=200,
            )
            repo.record_eod_day(
                conn,
                date(2026, 10, 2),
                status="other_day",
                n_records=0,
                now=now,
                file_date=date(2026, 10, 1),
                http_status=200,
            )
            # The index file is evidence too: here a Sunday special session.
            repo.record_eod_day(
                conn,
                date(2026, 2, 1),
                status="loaded",
                n_records=167,
                now=now,
                http_status=200,
                table=index_days,
            )
            from gats.db.schema import market_holidays

            conn.execute(
                market_holidays.insert().values(
                    segment="CM",
                    holiday_date=date(2026, 10, 20),
                    description="Dussehra",
                    available_at=now,
                    raw_doc_id=doc,
                    parser_version="v",
                )
            )
            cal = TradingCalendar.load(conn, today=date(2026, 10, 3))
            assert AsOf(conn, now).calendar().is_trading_day(date(2026, 10, 1))
        assert cal.is_trading_day(date(2026, 10, 1))
        assert not cal.is_trading_day(date(2026, 9, 14))  # copy of 11 Sep served
        # 2 Oct is the day before "today": one 'other_day' answer is not yet
        # final, so the weekday rule applies... unless the holiday list says so.
        assert cal.is_trading_day(date(2026, 10, 2))
        assert not cal.is_trading_day(date(2026, 10, 20))
        assert cal.is_special_session(date(2026, 2, 1))
        assert cal.coverage == (date(2026, 2, 1), date(2026, 10, 1))


class TestHolidaySource:
    def test_parse_real_shape(self) -> None:
        payload = json.dumps(
            {
                "CM": [
                    {
                        "tradingDate": "02-Oct-2026",
                        "weekDay": "Friday",
                        "description": "Mahatma Gandhi Jayanti",
                        "morning_session": None,
                        "evening_session": None,
                        "Sr_no": 15,
                    },
                    {
                        "tradingDate": "08-Nov-2026",
                        "weekDay": "Sunday",
                        "description": "Diwali Laxmi Pujan*",
                        "morning_session": None,
                        "evening_session": None,
                        "Sr_no": 17,
                    },
                ],
                "FO": [{"tradingDate": "02-Oct-2026", "description": "x"}],
            }
        ).encode()
        parsed = nse_holidays.parse_holidays(payload)
        assert [(r.segment, r.holiday_date) for r in parsed.records] == [
            ("CM", date(2026, 10, 2)),
            ("CM", date(2026, 11, 8)),
            ("FO", date(2026, 10, 2)),
        ]

    def test_wrong_shapes(self) -> None:
        with pytest.raises(PayloadError):
            nse_holidays.parse_holidays(b"<html></html>")
        with pytest.raises(PayloadError):
            nse_holidays.parse_holidays(b'{"FO": []}')

    @respx.mock
    async def test_ingest_is_cumulative(self, svc: Services) -> None:
        respx.get(svc.settings.nse_home_url).mock(return_value=httpx.Response(200))
        route = respx.get(HOLIDAYS_URL).mock(
            return_value=httpx.Response(
                200,
                content=json.dumps(
                    {"CM": [{"tradingDate": "02-Oct-2026", "description": "Gandhi Jayanti"}]}
                ).encode(),
            )
        )
        first = await ingest_nse_holidays(svc, job="t")
        assert first.ok and first.n_new == 1
        assert route.calls[0].request.url.params["type"] == "trading"
        second = await ingest_nse_holidays(svc, job="t")
        assert second.ok and second.n_new == 0


def test_settings_hours_match_the_cited_source() -> None:
    from gats.config import Settings

    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert (s.session_open_ist, s.session_close_ist) == (time(9, 15), time(15, 30))
