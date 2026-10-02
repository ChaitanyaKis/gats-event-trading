"""Reference data (M2): BSE scrip master and type-2 versioning."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import Connection, Engine, select

from gats.db import repo
from gats.db.schema import bse_scrips, refdata_snapshots
from gats.ingest import Services, reparse_kind
from gats.rawstore import RawStore
from gats.recorder import RefdataJob
from gats.refdata.coverage import bse_scrip_isin_coverage
from gats.refdata.ingest import BSE_SCRIP_ATTRS, ingest_bse_scrips
from gats.refdata.versions import apply_snapshot
from gats.sources import bse_scrips as src
from gats.sources.models import AnnouncementRecord
from tests.conftest import FakeClock

REAL = Path(__file__).parent / "fixtures" / "real" / "bse_scrips_2026-10-02.json"
SCRIPS_URL = "https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w"
T0 = datetime(2026, 10, 1, 3, 0, tzinfo=UTC)
D1, D2, D3 = date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3)


def scrip(code: str, **kw: Any) -> dict[str, Any]:
    row: dict[str, Any] = {a: None for a in BSE_SCRIP_ATTRS}
    row.update(scrip_code=code, symbol=f"S{code}", isin=f"INE{code}01010", status="Active")
    row.update(kw)
    return row


def raw_doc(conn: Connection, store: RawStore, tag: str) -> str:
    return repo.save_raw(
        conn,
        store,
        tag.encode(),
        kind="test",
        source="BSE",
        url="u",
        content_type=None,
        fetched_at=T0,
    )


def apply(conn: Connection, store: RawStore, day: date, rows: list[dict[str, Any]]) -> Any:
    return apply_snapshot(
        conn,
        bse_scrips,
        kind="bse_scrips",
        key="scrip_code",
        attrs=BSE_SCRIP_ATTRS,
        as_of=day,
        records=rows,
        available_at=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
        raw_doc_id=raw_doc(conn, store, f"{day}{len(rows)}{rows[:1]}"),
        parser_version="v",
    )


def versions(conn: Connection, code: str) -> list[tuple[date, date | None, str | None]]:
    return [
        (r.valid_from, r.valid_to, r.isin)
        for r in conn.execute(
            select(bse_scrips).where(bse_scrips.c.scrip_code == code).order_by("valid_from")
        )
    ]


class TestVersions:
    def test_first_snapshot_inserts_and_is_logged(self, engine: Engine, store: RawStore) -> None:
        with engine.begin() as conn:
            stats = apply(conn, store, D1, [scrip("500001"), scrip("500002")])
            assert stats.inserted == 2 and stats.n_changes == 2
            assert versions(conn, "500001") == [(D1, None, "INE50000101010")]
            log = conn.execute(select(refdata_snapshots)).one()
        assert (log.kind, log.as_of_date, log.n_records, log.n_changes) == (
            "bse_scrips",
            D1,
            2,
            2,
        )

    def test_unchanged_snapshot_adds_nothing(self, engine: Engine, store: RawStore) -> None:
        with engine.begin() as conn:
            apply(conn, store, D1, [scrip("500001")])
            stats = apply(conn, store, D2, [scrip("500001")])
            assert (stats.unchanged, stats.n_changes) == (1, 0)
            assert versions(conn, "500001") == [(D1, None, "INE50000101010")]

    def test_change_closes_old_version(self, engine: Engine, store: RawStore) -> None:
        with engine.begin() as conn:
            apply(conn, store, D1, [scrip("500001")])
            stats = apply(conn, store, D2, [scrip("500001", isin="INE50000101028")])
            assert stats.changed == 1
            assert versions(conn, "500001") == [
                (D1, D2, "INE50000101010"),
                (D2, None, "INE50000101028"),
            ]

    def test_reapplying_a_snapshot_is_idempotent(self, engine: Engine, store: RawStore) -> None:
        with engine.begin() as conn:
            apply(conn, store, D1, [scrip("500001")])
            apply(conn, store, D2, [scrip("500001", isin="INE50000101028")])
            before = versions(conn, "500001")
            stats = apply(conn, store, D2, [scrip("500001", isin="INE50000101028")])
            assert stats.n_changes == 0 and versions(conn, "500001") == before

    def test_replaying_history_in_order_rewrites_in_place(
        self, engine: Engine, store: RawStore
    ) -> None:
        # A parser fix replayed by `gats reparse`: D1 now parses differently.
        with engine.begin() as conn:
            apply(conn, store, D1, [scrip("500001", status="Active")])
            apply(conn, store, D2, [scrip("500001", status="Suspended")])
            stats = apply(conn, store, D1, [scrip("500001", status="ACTIVE")])
            assert stats.rewritten == 1
            rows = conn.execute(
                select(bse_scrips.c.valid_from, bse_scrips.c.valid_to, bse_scrips.c.status)
                .where(bse_scrips.c.scrip_code == "500001")
                .order_by(bse_scrips.c.valid_from)
            ).all()
        assert [tuple(r) for r in rows] == [(D1, D2, "ACTIVE"), (D2, None, "Suspended")]

    def test_entity_first_seen_in_an_older_replay_ends_at_next_version(
        self, engine: Engine, store: RawStore
    ) -> None:
        with engine.begin() as conn:
            apply(conn, store, D3, [scrip("500001")])
            apply(conn, store, D2, [scrip("500001", isin="INE50000101028")])
            assert versions(conn, "500001") == [
                (D2, D3, "INE50000101028"),
                (D3, None, "INE50000101010"),
            ]

    def test_dropped_entity_is_closed(self, engine: Engine, store: RawStore) -> None:
        many = [scrip(str(500000 + i)) for i in range(40)]
        with engine.begin() as conn:
            apply(conn, store, D1, many)
            stats = apply(conn, store, D2, many[1:])
            assert stats.closed == 1
            assert versions(conn, "500000") == [(D1, D2, "INE50000001010")]

    def test_truncated_snapshot_closes_nothing(self, engine: Engine, store: RawStore) -> None:
        many = [scrip(str(500000 + i)) for i in range(40)]
        with engine.begin() as conn:
            apply(conn, store, D1, many)
            stats = apply(conn, store, D2, many[:10])
            assert stats.closed == 0 and "truncated" in stats.warnings[0]
            assert versions(conn, "500039") == [(D1, None, "INE50003901010")]


class TestParser:
    def test_real_sample(self) -> None:
        parsed = src.parse_scrips(REAL.read_bytes())
        assert len(parsed.records) == 30
        assert parsed.meta["unknown_fields"] == []
        by_code = {r.scrip_code: r for r in parsed.records}
        abb = by_code["500002"]
        assert (abb.symbol, abb.isin, abb.status, abb.group, abb.face_value) == (
            "ABB",
            "INE117A01022",
            "Active",
            "A",
            2.0,
        )
        assert {r.status for r in parsed.records} == {"Active", "Delisted", "Suspended", "N"}
        # NA, empty and malformed ISINs all become None; malformed ones warn.
        assert by_code["500011"].isin is None and by_code["750255"].isin is None
        assert by_code["500181"].isin is None and by_code["750516"].isin is None
        assert sorted(w.split(":")[0] for w in parsed.warnings) == [
            "row 21 (500181)",
            "row 28 (750516)",
        ]

    def test_block_page_and_wrong_shape(self) -> None:
        with pytest.raises(src.PayloadError):
            src.parse_scrips(b"<HTML><HEAD><TITLE>Access Denied</TITLE>")
        with pytest.raises(src.PayloadError):
            src.parse_scrips(b'{"Table": []}')

    def test_duplicate_codes_keep_first(self) -> None:
        rows = [{"SCRIP_CD": "500001", "Status": "Active"}, {"SCRIP_CD": "500001"}]
        parsed = src.parse_scrips(json.dumps(rows).encode())
        assert len(parsed.records) == 1 and "duplicate" in parsed.warnings[0]


class TestIngest:
    @respx.mock
    async def test_snapshot_job_fetches_once_per_day(self, svc: Services, clock: FakeClock) -> None:
        route = respx.get(SCRIPS_URL).mock(
            return_value=httpx.Response(200, content=REAL.read_bytes())
        )
        job = RefdataJob(
            "bse_scrips",
            src.KIND,
            1800,
            svc.settings.daily_snapshot_after_ist,
            lambda s: ingest_bse_scrips(s, job="bse_scrips"),
        )
        first = await job.run_once(svc)
        assert first.ok and first.n_new == 30 and route.call_count == 1
        second = await job.run_once(svc)
        assert second.meta == {"skipped": "already have today"} and route.call_count == 1
        assert route.calls[0].request.url.params["status"] == ""  # delisted included

        clock.now += timedelta(days=1)
        third = await job.run_once(svc)
        assert third.ok and third.n_new == 0 and route.call_count == 2

    async def test_job_waits_for_daily_window(self, svc: Services, clock: FakeClock) -> None:
        clock.now = datetime(2026, 10, 2, 1, 0, tzinfo=UTC)  # 06:30 IST
        job = RefdataJob("bse_scrips", src.KIND, 1800, svc.settings.daily_snapshot_after_ist, None)  # type: ignore[arg-type]
        assert (await job.run_once(svc)).meta == {"skipped": "before daily window"}

    @respx.mock
    async def test_block_page_is_kept_as_bad_payload(self, svc: Services) -> None:
        respx.get(SCRIPS_URL).mock(
            return_value=httpx.Response(200, content=b"<HTML><TITLE>Access Denied</TITLE>")
        )
        outcome = await ingest_bse_scrips(svc, job="t")
        assert not outcome.ok and "HTML" in (outcome.error or "")
        with svc.engine.begin() as conn:
            assert repo.raw_documents_of_kind(conn, "bad_bse_scrips")
            assert conn.execute(select(bse_scrips)).first() is None

    @respx.mock
    async def test_reparse_rebuilds_the_same_versions(self, svc: Services) -> None:
        respx.get(SCRIPS_URL).mock(return_value=httpx.Response(200, content=REAL.read_bytes()))
        await ingest_bse_scrips(svc, job="t")
        with svc.engine.begin() as conn:
            before = conn.execute(select(bse_scrips).order_by("scrip_code")).all()
        stats = reparse_kind(svc, src.KIND)
        assert stats["documents"] == 1 and stats["errors"] == 0
        with svc.engine.begin() as conn:
            assert conn.execute(select(bse_scrips).order_by("scrip_code")).all() == before


def test_coverage_counts_filings_and_codes(engine: Engine, store: RawStore) -> None:
    def ann(ann_id: str, code: str) -> AnnouncementRecord:
        return AnnouncementRecord(
            source="BSE",
            source_ann_id=ann_id,
            symbol=None,
            scrip_code=code,
            isin=None,
            company_name=f"Co {code}",
            category=None,
            subcategory=None,
            subject=None,
            details=None,
            attachment_url=None,
            exch_submitted_ts=None,
            exch_disseminated_ts=T0,
            event_ts=T0,
        )

    with engine.begin() as conn:
        apply(conn, store, D1, [scrip("500001"), scrip("500002", isin=None)])
        repo.insert_announcements(
            conn,
            [ann("a", "500001"), ann("b", "500001"), ann("c", "500002"), ann("d", "999999")],
            raw_doc_id=raw_doc(conn, store, "anns"),
            parser_version="v",
            mode="backfill",
            fetched_at=T0,
            now=T0,
        )
        cov = bse_scrip_isin_coverage(conn, T0 - timedelta(days=1))
    assert (cov.resolved_filings, cov.n_filings, cov.resolved_ids, cov.n_ids) == (2, 4, 1, 3)
    assert [u[0] for u in cov.unresolved] == ["500002", "999999"]
