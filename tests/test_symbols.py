"""NSE symbol history (T2.2)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import Engine, select

from gats.db import repo
from gats.db.schema import nse_symbol_changes
from gats.ingest import Services, reparse_kind
from gats.rawstore import RawStore
from gats.refdata.ingest import ingest_nse_symbol_changes
from gats.refdata.symbols import Change, SymbolHistory, SymbolWindow, store_changes
from gats.sources import nse, nse_archives, nse_symbols
from gats.sources.models import PayloadError
from gats.timeutil import to_ist

REAL = Path(__file__).parent / "fixtures" / "real"
CHANGES = REAL / "nse_symbolchange_2026-10-02.csv"
URL = "https://nsearchives.nseindia.com/content/equities/symbolchange.csv"
TODAY = date(2026, 10, 2)


def real_history() -> SymbolHistory:
    parsed = nse_symbols.parse_symbol_changes(CHANGES.read_bytes())
    return SymbolHistory(
        [Change(r.old_symbol, r.new_symbol, r.effective_date) for r in parsed.records]
    )


class TestParser:
    def test_real_file(self) -> None:
        parsed = nse_symbols.parse_symbol_changes(CHANGES.read_bytes())
        assert parsed.warnings == []
        assert len(parsed.records) == 12
        zomato = next(r for r in parsed.records if r.old_symbol == "ZOMATO")
        assert (zomato.new_symbol, zomato.effective_date, zomato.company_name) == (
            "ETERNAL",
            date(2025, 4, 9),
            "ETERNAL LIMITED",
        )
        assert any(r.company_name is None for r in parsed.records)

    def test_header_row_is_skipped_with_warning(self) -> None:
        payload = b"Company,Old,New,Date\nX Ltd,OLDX,NEWX,01-JAN-2020\n"
        parsed = nse_symbols.parse_symbol_changes(payload)
        assert [r.new_symbol for r in parsed.records] == ["NEWX"]
        assert "unparseable date" in parsed.warnings[0]

    def test_unusable_file_fails_loudly(self) -> None:
        with pytest.raises(PayloadError):
            nse_symbols.parse_symbol_changes(b"<html>blocked</html>")
        with pytest.raises(PayloadError):
            nse_symbols.parse_symbol_changes(b"a,b\nc,d\n")


class TestResolver:
    def test_real_rename_resolves_both_ways(self) -> None:
        h = real_history()
        assert h.symbol_on("ETERNAL", TODAY, date(2025, 4, 8)) == "ZOMATO"
        assert h.symbol_on("ETERNAL", TODAY, date(2025, 4, 9)) == "ETERNAL"  # effective day
        assert h.symbol_on("ZOMATO", date(2025, 4, 8), TODAY) == "ETERNAL"
        assert h.symbol_on("RELIANCE", TODAY, date(2001, 1, 1)) == "RELIANCE"

    def test_chains_are_followed(self) -> None:
        h = real_history()
        assert h.symbol_on("TMPV", TODAY, date(2010, 1, 1)) == "TATAMOTORS"
        assert h.symbol_on("TMPV", TODAY, date(2003, 12, 25)) == "TELCO"
        assert h.symbol_on("TELCO", date(2000, 1, 1), TODAY) == "TMPV"
        assert h.windows("LTM", TODAY) == [
            SymbolWindow("LTI", None, date(2022, 12, 5)),
            SymbolWindow("LTIM", date(2022, 12, 5), date(2026, 2, 27)),
            SymbolWindow("LTM", date(2026, 2, 27), None),
        ]

    def test_self_maps_are_ignored(self) -> None:
        # INDINFR -> INTERISE plus INTERISE -> INTERISE on the same day.
        h = real_history()
        assert h.symbol_on("INTERISE", TODAY, date(2024, 1, 1)) == "INDINFR"

    def test_announcement_symbol_maps_to_the_price_file_of_its_day(self) -> None:
        """The acceptance case: a 2024 Zomato filing arrives as ETERNAL, the
        2025-04-08 price file says ZOMATO, the 2025-04-09 file says ETERNAL."""
        filing = nse.parse_announcements((REAL / "nse_ann_2024-08-01_eternal.json").read_bytes())
        assert {r.symbol for r in filing.records} == {"ETERNAL"}
        h = real_history()
        for day, name in ((date(2025, 4, 8), "2025-04-08"), (date(2025, 4, 9), "2025-04-09")):
            eod = nse_archives.parse_eod(
                (REAL / f"nse_eod_{name}_excerpt.csv").read_bytes(), trade_date=day
            )
            symbols_that_day = {r.symbol for r in eod.records}
            assert h.symbol_on("ETERNAL", TODAY, day) in symbols_that_day
        event_day = to_ist(filing.records[0].event_ts).date()  # type: ignore[arg-type]
        assert h.symbol_on("ETERNAL", TODAY, event_day) == "ZOMATO"


class TestStore:
    def test_available_at_rule_and_idempotence(self, engine: Engine, store: RawStore) -> None:
        parsed = nse_symbols.parse_symbol_changes(CHANGES.read_bytes())
        fetched = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
        future = nse_symbols.SymbolChangeRecord("OLDF", "NEWF", date(2026, 10, 9), "F Ltd")
        with engine.begin() as conn:
            doc = repo.save_raw(
                conn,
                store,
                b"x",
                kind="t",
                source="NSE",
                url="u",
                content_type=None,
                fetched_at=fetched,
            )
            n = store_changes(
                conn,
                [*parsed.records, future],
                fetched_at=fetched,
                raw_doc_id=doc,
                parser_version="v",
            )
            again = store_changes(
                conn, parsed.records, fetched_at=fetched, raw_doc_id=doc, parser_version="v"
            )
            rows = {r.old_symbol: r.available_at for r in conn.execute(select(nse_symbol_changes))}
        assert (n, again) == (13, 0)
        # Past change: knowable from 00:00 IST on its effective date.
        assert rows["ZOMATO"] == datetime(2025, 4, 8, 18, 30, tzinfo=UTC)
        # Change announced ahead of time: knowable from the fetch.
        assert rows["OLDF"] == fetched

    def test_point_in_time_load(self, engine: Engine, store: RawStore) -> None:
        parsed = nse_symbols.parse_symbol_changes(CHANGES.read_bytes())
        with engine.begin() as conn:
            doc = repo.save_raw(
                conn,
                store,
                b"x",
                kind="t",
                source="NSE",
                url="u",
                content_type=None,
                fetched_at=datetime(2026, 10, 2, tzinfo=UTC),
            )
            store_changes(
                conn,
                parsed.records,
                fetched_at=datetime(2026, 10, 2, tzinfo=UTC),
                raw_doc_id=doc,
                parser_version="v",
            )
            before = SymbolHistory.load(conn, as_of=datetime(2025, 1, 1, tzinfo=UTC))
            after = SymbolHistory.load(conn)
        # On 2025-01-01 nobody knew ZOMATO would become ETERNAL.
        assert before.symbol_on("ZOMATO", date(2024, 12, 31), TODAY) == "ZOMATO"
        assert after.symbol_on("ZOMATO", date(2024, 12, 31), TODAY) == "ETERNAL"


@respx.mock
async def test_ingest_and_reparse(svc: Services) -> None:
    respx.get(svc.settings.nse_home_url).mock(return_value=httpx.Response(200))
    respx.get(URL).mock(return_value=httpx.Response(200, content=CHANGES.read_bytes()))
    first = await ingest_nse_symbol_changes(svc, job="t")
    assert first.ok and first.n_new == 12
    second = await ingest_nse_symbol_changes(svc, job="t")
    assert second.ok and second.n_new == 0
    stats = reparse_kind(svc, nse_symbols.KIND)
    assert stats == {"documents": 1, "updated": 0, "inserted": 0, "errors": 0}
