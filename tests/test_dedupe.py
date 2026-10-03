"""Cross-exchange dedupe (T2.5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import Engine, select

from gats.db import repo
from gats.db.schema import announcement_event_group, announcement_security
from gats.ingest import Services
from gats.pit import AsOf
from gats.rawstore import RawStore
from gats.recorder import DedupeJob
from gats.refdata import dedupe
from gats.sources import bse, nse
from gats.sources.models import AnnouncementRecord

REAL = Path(__file__).parent / "fixtures" / "real"
T0 = datetime(2026, 9, 10, 5, 0, tzinfo=UTC)


def ns(**kw: Any) -> SimpleNamespace:
    base: dict[str, Any] = dict(
        category=None,
        subcategory=None,
        subject=None,
        details=None,
        company_name="Acme Ltd",
        attachment_size=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


class TestRules:
    def test_topic_tokens_drop_boilerplate_names_and_bse_prefix(self) -> None:
        bse_row = ns(
            source="BSE",
            category="Company Update",
            subcategory="Newspaper Publication",
            subject=(
                "Acme Ltd - 500001 - Announcement under Regulation 30 (LODR)-Newspaper Publication"
            ),
            details="Please find enclosed newspaper advertisement",
        )
        nse_row = ns(
            source="NSE",
            category="Copy of Newspaper Publication",
            details="Acme Limited has informed the Exchange about Copy of Newspaper Publication",
        )
        assert dedupe.topic_tokens(bse_row) == {"newspaper", "publication", "advertisement"}
        assert dedupe.topic_tokens(nse_row) == {"newspaper", "publication"}

    @pytest.mark.parametrize(
        ("nse_size", "bse_size", "expected"),
        [
            (round(165.57 * 1024), 169542, "kb"),  # real pair, "165.57 KB"
            (292116, 292116, "kb"),
            (round(1.18 * 1024**2), 1240090, "mb"),  # agrees only at MB precision
            (round(1.18 * 1024**2), 1300000, "no"),
            (222761, 236261, "no"),
            (None, 1000, None),
            (1000, None, None),
        ],
    )
    def test_size_agreement(
        self, nse_size: int | None, bse_size: int | None, expected: str | None
    ) -> None:
        assert dedupe.size_agreement(nse_size, bse_size) == expected

    def test_real_pairs_from_one_evening(self) -> None:
        """Both 2026-10-02 samples: same company (stand-in for the security
        link) on both exchanges. Every same-size pair is the same disclosure."""
        b = bse.parse_announcements(
            (REAL / "bse_ann_2026-10-02.json").read_bytes(), attachment_base="X/"
        ).records
        n = nse.parse_announcements((REAL / "nse_ann_2026-10-02.json").read_bytes()).records
        rows = [
            SimpleNamespace(
                id=i, key=r.company_name.lower().replace("limited", "ltd"), **vars_of(r)
            )
            for i, r in enumerate([*n, *b])
        ]
        by: dict[str, list[SimpleNamespace]] = {}
        for row in rows:
            by.setdefault(row.key.split(" ")[0], []).append(row)
        pairs = {
            (rows[p].source_ann_id, rows[q].source_ann_id)
            for members in by.values()
            for p, q in dedupe.match_security(members)
        }
        # Hindustan Aeronautics: identical 292,116-byte PDF, 4 minutes apart.
        assert ("106806115", "421f0ddd-b493-4936-a959-52023c249ab3") in pairs
        # Punjab National Bank: 428,524 vs 428,526 bytes, 10 s apart.
        assert ("106806111", "519e2dbd-6fbe-4d6b-b2f9-9d53bb32cc86") in pairs
        assert len(pairs) >= 12
        assert len({p for p, _ in pairs}) == len(pairs)  # one-to-one
        assert len({q for _, q in pairs}) == len(pairs)

    def test_two_filings_two_documents_pair_correctly(self) -> None:
        # Same company, same minute: a dividend record date and an AGM notice.
        rows = [
            ns(
                id=1,
                source="NSE",
                event_ts=T0,
                attachment_size=200_000,
                category="Record Date",
                details="Record date for dividend",
            ),
            ns(
                id=2,
                source="NSE",
                event_ts=T0,
                attachment_size=5_000_000,
                category="Shareholders meeting",
                details="Notice of Annual General Meeting",
            ),
            ns(
                id=3,
                source="BSE",
                event_ts=T0 + timedelta(minutes=3),
                attachment_size=5_000_100,
                category="AGM/EGM",
                subcategory="AGM",
                subject="Acme Ltd - 1 - Notice of AGM",
            ),
            ns(
                id=4,
                source="BSE",
                event_ts=T0 + timedelta(minutes=4),
                attachment_size=200_002,
                category="Corp. Action",
                subcategory="Record Date",
                subject="Acme Ltd - 1 - Dividend",
            ),
        ]
        assert sorted(dedupe.match_security(rows)) == [(1, 4), (2, 3)]

    def test_far_apart_or_unrelated_filings_stay_single(self) -> None:
        rows = [
            ns(id=1, source="NSE", event_ts=T0, attachment_size=100_000, category="Trading Window"),
            ns(
                id=2,
                source="BSE",
                event_ts=T0 + timedelta(minutes=5),
                attachment_size=300_000,
                category="Company Update",
                subcategory="Credit Rating",
            ),
            ns(
                id=3,
                source="BSE",
                event_ts=T0 + timedelta(hours=7),
                attachment_size=100_000,
                category="Insider Trading",
                subcategory="Trading Window",
            ),
        ]
        assert dedupe.match_security(rows) == []


def vars_of(record: AnnouncementRecord) -> dict[str, Any]:
    return {f: getattr(record, f) for f in record.__slots__}  # type: ignore[attr-defined]


def store_filing(
    conn: Any,
    store: RawStore,
    ann_id: str,
    source: str,
    at: datetime,
    size: int,
    available: datetime | None = None,
    security_id: int | None = 7,
) -> int:
    doc = repo.save_raw(
        conn,
        store,
        ann_id.encode(),
        kind="t",
        source=source,
        url="u",
        content_type=None,
        fetched_at=at,
    )
    record = AnnouncementRecord(
        source=source,
        source_ann_id=ann_id,
        symbol=None,
        scrip_code=None,
        isin=None,
        company_name="Acme Ltd",
        category="Board Meeting",
        subcategory="Outcome",
        subject="Outcome of Board Meeting",
        details="Outcome of board meeting",
        attachment_url="u",
        exch_submitted_ts=None,
        exch_disseminated_ts=at,
        event_ts=at,
        attachment_size=size,
    )
    seen = available or at
    repo.insert_announcements(
        conn, [record], raw_doc_id=doc, parser_version="v", mode="live", fetched_at=seen, now=seen
    )
    from gats.db.schema import announcements

    row_id = int(
        conn.execute(
            select(announcements.c.id).where(announcements.c.source_ann_id == ann_id)
        ).scalar_one()
    )
    if security_id is not None:
        conn.execute(
            announcement_security.insert().values(
                announcement_id=row_id,
                security_id=security_id,
                method="t",
                build_id=1,
                linked_at=seen,
            )
        )
    return row_id


@pytest.fixture
def conn(engine: Engine) -> Any:
    with engine.begin() as connection:
        yield connection


def groups(conn: Any) -> dict[int, int]:
    return {
        r[0]: r[1]
        for r in conn.execute(
            select(
                announcement_event_group.c.announcement_id,
                announcement_event_group.c.event_group_id,
            )
        )
    }


class TestGrouping:
    def test_pair_gets_the_smaller_id_and_reruns_are_stable(
        self, conn: Any, store: RawStore
    ) -> None:
        b = store_filing(conn, store, "b", "BSE", T0, 400_000)
        n = store_filing(conn, store, "n", "NSE", T0 + timedelta(minutes=4), 400_003)
        lone = store_filing(conn, store, "x", "NSE", T0 + timedelta(hours=3), 999_999)
        stats = dedupe.group_range(conn, T0 - timedelta(days=1), T0 + timedelta(days=1), T0)
        assert (stats.filings, stats.pairs, stats.singles) == (3, 1, 1)
        assert groups(conn) == {b: min(b, n), n: min(b, n), lone: lone}
        dedupe.group_range(conn, T0 - timedelta(days=1), T0 + timedelta(days=1), T0)
        assert groups(conn) == {b: min(b, n), n: min(b, n), lone: lone}

    def test_pair_straddling_the_range_edge_is_found_from_either_side(
        self, conn: Any, store: RawStore
    ) -> None:
        b = store_filing(conn, store, "b", "BSE", T0 - timedelta(minutes=2), 400_000)
        n = store_filing(conn, store, "n", "NSE", T0 + timedelta(minutes=2), 400_003)
        dedupe.group_range(conn, T0, T0 + timedelta(hours=1), T0)  # only n in range
        assert groups(conn) == {n: min(b, n)}

    def test_unlinked_filings_are_single_events(self, conn: Any, store: RawStore) -> None:
        a = store_filing(conn, store, "b", "BSE", T0, 400_000, security_id=None)
        c = store_filing(conn, store, "n", "NSE", T0, 400_000, security_id=None)
        dedupe.group_range(conn, T0 - timedelta(hours=1), T0 + timedelta(hours=1), T0)
        assert groups(conn) == {a: a, c: c}


class TestAsOfEvents:
    def test_event_time_is_the_earliest_member(self, conn: Any, store: RawStore) -> None:
        store_filing(conn, store, "b", "BSE", T0, 400_000)
        store_filing(conn, store, "n", "NSE", T0 + timedelta(minutes=4), 400_003)
        dedupe.group_range(conn, T0 - timedelta(days=1), T0 + timedelta(days=1), T0)
        (event,) = AsOf(conn, T0 + timedelta(hours=1)).events_since(T0 - timedelta(days=1))
        assert (event.event_ts, event.n_filings, event.security_id) == (T0, 2, 7)

    def test_later_twin_cannot_leak_into_the_past(self, conn: Any, store: RawStore) -> None:
        # NSE filing seen at 10:30; its BSE twin was disseminated earlier
        # (10:28) but this system only fetched it at 10:40.
        n_seen = T0 + timedelta(minutes=30)
        store_filing(conn, store, "n", "NSE", n_seen, 400_003)
        store_filing(
            conn,
            store,
            "b",
            "BSE",
            T0 + timedelta(minutes=28),
            400_000,
            available=T0 + timedelta(minutes=40),
        )
        dedupe.group_range(conn, T0 - timedelta(days=1), T0 + timedelta(days=1), T0)
        at_1035 = AsOf(conn, T0 + timedelta(minutes=35)).events_since(T0 - timedelta(days=1))
        assert [(e.event_ts, e.n_filings) for e in at_1035] == [(n_seen, 1)]
        at_1045 = AsOf(conn, T0 + timedelta(minutes=45)).events_since(T0 - timedelta(days=1))
        assert [(e.event_ts, e.available_at, e.n_filings) for e in at_1045] == [
            (T0 + timedelta(minutes=28), n_seen, 2)
        ]
        # Already reported at 10:35, so a 10:35 -> 10:45 window does not repeat it.
        assert AsOf(conn, T0 + timedelta(minutes=45)).events_since(T0 + timedelta(minutes=35)) == []


async def test_dedupe_job(svc: Services) -> None:
    job = DedupeJob("dedupe", 300, 2)
    outcome = await job.run_once(svc)
    assert outcome.ok and outcome.meta == {"pairs": 0}
