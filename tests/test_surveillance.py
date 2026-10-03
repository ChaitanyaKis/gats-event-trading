"""Surveillance lists (T2.9): ASM, GSM and trade-for-trade."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import Engine, select

from gats.db import repo
from gats.db.schema import surveillance_versions
from gats.ingest import Services
from gats.pit import AsOf
from gats.rawstore import RawStore
from gats.refdata.ingest import apply_nse_asm, apply_nse_gsm, ingest_nse_surveillance
from gats.sources import nse_surveillance
from gats.sources.models import BandRecord, PayloadError

REAL = Path(__file__).parent / "fixtures" / "real"
ASM = (REAL / "nse_asm_2026-10-03.json").read_bytes()
GSM = (REAL / "nse_gsm_2026-10-03.json").read_bytes()
D1, D2 = date(2026, 10, 3), date(2026, 10, 5)
T1 = datetime(2026, 10, 3, 3, 0, tzinfo=UTC)
T2 = datetime(2026, 10, 5, 3, 0, tzinfo=UTC)


class TestParsers:
    def test_asm(self) -> None:
        parsed = nse_surveillance.parse_asm(ASM)
        assert parsed.warnings == []
        assert [r.list_name for r in parsed.records].count("LTASM") == 9
        assert [r.list_name for r in parsed.records].count("STASM") == 5
        first = parsed.records[0]
        assert (first.key, first.stage, first.isin, first.since) == (
            "LTASM:A2ZINFRA",
            "Stage I",
            "INE619I01012",
            date(2026, 10, 2),
        )
        assert {r.stage for r in parsed.records} >= {"Stage I", "Stage IV"}

    def test_gsm(self) -> None:
        parsed = nse_surveillance.parse_gsm(GSM)
        assert parsed.warnings == []
        agstra = parsed.records[0]
        assert (agstra.key, agstra.stage, agstra.surv_code, agstra.since) == (
            "GSM:AGSTRA",
            "LVIII",
            "IBC I & GSM 0 (58)",
            date(2026, 10, 1),  # gsmTime 01-Oct-2026 08:07:02 IST
        )

    def test_wrong_shapes(self) -> None:
        with pytest.raises(PayloadError):
            nse_surveillance.parse_asm(b"[]")
        with pytest.raises(PayloadError):
            nse_surveillance.parse_gsm(b'{"data": []}')
        with pytest.raises(PayloadError):
            nse_surveillance.parse_asm(b"<html>x</html>")


def _doc(conn: Any, store: RawStore, tag: bytes) -> str:
    return repo.save_raw(
        conn, store, tag, kind="t", source="NSE", url="u", content_type=None, fetched_at=T1
    )


def versions(conn: Any, key: str) -> list[tuple[date, date | None]]:
    return [
        (r.valid_from, r.valid_to)
        for r in conn.execute(
            select(surveillance_versions)
            .where(surveillance_versions.c.entity_key == key)
            .order_by(surveillance_versions.c.valid_from)
        )
    ]


def without(payload: bytes, section: str, symbol: str) -> bytes:
    data = json.loads(payload)
    data[section]["data"] = [r for r in data[section]["data"] if r["symbol"] != symbol]
    return json.dumps(data).encode()


class TestVersions:
    def test_leaving_a_list_closes_the_entry(self, engine: Engine, store: RawStore) -> None:
        with engine.begin() as conn:
            apply_nse_asm(conn, ASM, D1, _doc(conn, store, b"a"), T1)
            stats = apply_nse_asm(
                conn, without(ASM, "longterm", "A2ZINFRA"), D2, _doc(conn, store, b"b"), T2
            )
            assert stats.closed == 1
            assert versions(conn, "LTASM:A2ZINFRA") == [(D1, D2)]
            assert versions(conn, "LTASM:AEPL") == [(D1, None)]

    def test_asm_snapshot_does_not_close_gsm_entries(self, engine: Engine, store: RawStore) -> None:
        with engine.begin() as conn:
            apply_nse_gsm(conn, GSM, D1, _doc(conn, store, b"g"), T1)
            apply_nse_asm(conn, ASM, D2, _doc(conn, store, b"a"), T2)
            assert versions(conn, "GSM:AGSTRA") == [(D1, None)]

    def test_empty_list_closes_nothing(self, engine: Engine, store: RawStore) -> None:
        empty = json.dumps({"longterm": {"data": []}, "shortterm": {"data": []}}).encode()
        with engine.begin() as conn:
            apply_nse_asm(conn, ASM, D1, _doc(conn, store, b"a"), T1)
            stats = apply_nse_asm(conn, empty, D2, _doc(conn, store, b"e"), T2)
            assert stats.closed == 0 and "truncated" in stats.warnings[0]
            assert versions(conn, "LTASM:A2ZINFRA") == [(D1, None)]


class TestAsOfView:
    def test_lists_trade_for_trade_and_point_in_time(self, engine: Engine, store: RawStore) -> None:
        with engine.begin() as conn:
            apply_nse_asm(conn, ASM, D1, _doc(conn, store, b"a"), T1)
            apply_nse_gsm(conn, GSM, D1, _doc(conn, store, b"g"), T1)
            repo.upsert_bands(
                conn,
                D1,
                [
                    BandRecord("AGSTRA", "BZ", "2", "GSM STAGE - 0"),
                    BandRecord("A2ZINFRA", "EQ", "5", None),
                    BandRecord("BOTHCO", "EQ", "20", None),
                    BandRecord("BOTHCO", "BE", "20", None),
                ],
                raw_doc_id=_doc(conn, store, b"b"),
                parser_version="v",
                available_at=T1,
            )
            now = AsOf(conn, T1 + timedelta(hours=1))
            agstra = now.surveillance("agstra")
            assert agstra["lists"] == {"GSM": "LVIII"}
            assert agstra["gsm_remark"] == "GSM STAGE - 0"
            assert agstra["trade_for_trade"] is True  # BZ only
            assert now.surveillance("A2ZINFRA")["lists"] == {"LTASM": "Stage I"}
            assert now.surveillance("A2ZINFRA")["trade_for_trade"] is False
            # Listed in EQ as well: rolling settlement is available.
            assert now.surveillance("BOTHCO")["trade_for_trade"] is False
            # Before the lists were fetched, nothing was known.
            before = AsOf(conn, T1 - timedelta(minutes=1)).surveillance("A2ZINFRA")
            assert before["lists"] == {} and before["band_date"] is None


@respx.mock
async def test_ingest_both_lists(svc: Services) -> None:
    respx.get(svc.settings.nse_home_url).mock(return_value=httpx.Response(200))
    respx.get(svc.settings.nse_asm_url).mock(return_value=httpx.Response(200, content=ASM))
    respx.get(svc.settings.nse_gsm_url).mock(return_value=httpx.Response(200, content=GSM))
    asm, gsm = await ingest_nse_surveillance(svc, job="t")
    assert (asm.ok, asm.n_new, gsm.ok, gsm.n_new) == (True, 14, True, 6)
