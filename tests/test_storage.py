from __future__ import annotations

import gzip
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import Engine, select

from gats.db import repo
from gats.db.schema import announcements, eod_prices, raw_documents
from gats.pit import AsOf
from gats.rawstore import RawStore
from gats.sources.models import AnnouncementRecord, EodRecord

T0 = datetime(2026, 9, 25, 4, 45, tzinfo=UTC)


def make_record(ann_id: str, event_ts: datetime | None = T0, **kw: object) -> AnnouncementRecord:
    base = AnnouncementRecord(
        source="BSE",
        source_ann_id=ann_id,
        symbol=None,
        scrip_code="500325",
        isin=None,
        company_name="Example",
        category="Company Update",
        subcategory=None,
        subject="subject",
        details=None,
        attachment_url="https://x/a.pdf",
        exch_submitted_ts=None,
        exch_disseminated_ts=event_ts,
        event_ts=event_ts,
    )
    return replace(base, **kw)  # type: ignore[arg-type]


def save_doc(engine: Engine, store: RawStore, data: bytes = b"payload") -> str:
    with engine.begin() as conn:
        return repo.save_raw(
            conn,
            store,
            data,
            kind="bse_ann",
            source="BSE",
            url="u",
            content_type=None,
            fetched_at=T0,
            meta={"mode": "live"},
        )


class TestRawStore:
    def test_roundtrip_and_dedupe(self, store: RawStore) -> None:
        doc_a, path_a = store.put(b"hello")
        doc_b, path_b = store.put(b"hello")
        assert (doc_a, path_a) == (doc_b, path_b)
        assert store.get(doc_a) == b"hello"

    def test_detects_corruption(self, store: RawStore) -> None:
        doc_id, rel = store.put(b"hello")
        with gzip.open(store.root / rel, "wb") as gz:
            gz.write(b"tampered")
        with pytest.raises(ValueError, match="corrupt"):
            store.get(doc_id)


class TestAnnouncements:
    def test_insert_is_idempotent(self, engine: Engine, store: RawStore) -> None:
        doc = save_doc(engine, store)
        records = [make_record("a"), make_record("b"), make_record("a")]
        with engine.begin() as conn:
            first = repo.insert_announcements(
                conn,
                records,
                raw_doc_id=doc,
                parser_version="v1",
                mode="live",
                fetched_at=T0,
                now=T0,
            )
            second = repo.insert_announcements(
                conn,
                records,
                raw_doc_id=doc,
                parser_version="v1",
                mode="live",
                fetched_at=T0,
                now=T0,
            )
            count = conn.execute(select(announcements.c.id)).all()
        assert [r.source_ann_id for r in first] == ["a", "b"]
        assert second == []
        assert len(count) == 2

    def test_available_at_rules(self, engine: Engine, store: RawStore) -> None:
        doc = save_doc(engine, store)
        fetched = T0 + timedelta(seconds=40)
        with engine.begin() as conn:
            repo.insert_announcements(
                conn,
                [make_record("live")],
                raw_doc_id=doc,
                parser_version="v1",
                mode="live",
                fetched_at=fetched,
                now=fetched,
            )
            repo.insert_announcements(
                conn,
                [make_record("back")],
                raw_doc_id=doc,
                parser_version="v1",
                mode="backfill",
                fetched_at=fetched,
                now=fetched,
            )
            # Clock skew: fetched before the event. available_at must not precede the event.
            repo.insert_announcements(
                conn,
                [make_record("skew")],
                raw_doc_id=doc,
                parser_version="v1",
                mode="live",
                fetched_at=T0 - timedelta(seconds=5),
                now=fetched,
            )
            rows = {r.source_ann_id: r for r in conn.execute(select(announcements))}
        assert rows["live"].available_at == fetched
        assert rows["back"].available_at == T0
        assert rows["skew"].available_at == T0
        assert rows["live"].attachment_status == "pending"
        assert rows["live"].available_at.tzinfo is UTC

    def test_naive_datetime_is_rejected(self, engine: Engine, store: RawStore) -> None:
        doc = save_doc(engine, store)
        with pytest.raises(Exception, match="naive"), engine.begin() as conn:
            repo.insert_announcements(
                conn,
                [make_record("n")],
                raw_doc_id=doc,
                parser_version="v1",
                mode="live",
                fetched_at=datetime(2026, 9, 25),
                now=T0,
            )

    def test_reparse_updates_fields_but_not_ingest_times(
        self, engine: Engine, store: RawStore
    ) -> None:
        doc = save_doc(engine, store)
        fetched = T0 + timedelta(seconds=30)
        with engine.begin() as conn:
            repo.insert_announcements(
                conn,
                [make_record("a", attachment_url=None)],
                raw_doc_id=doc,
                parser_version="v1",
                mode="live",
                fetched_at=fetched,
                now=fetched,
            )
        fixed = [
            make_record("a", subject="fixed", attachment_url="https://x/new.pdf"),
            make_record("dropped-by-v1"),
        ]
        with engine.begin() as conn:
            updated, inserted = repo.reparse_announcements(
                conn,
                fixed,
                raw_doc_id=doc,
                parser_version="v2",
                mode="live",
                fetched_at=fetched,
                now=T0 + timedelta(days=3),
            )
            rows = {r.source_ann_id: r for r in conn.execute(select(announcements))}
        assert (updated, inserted) == (1, 1)
        assert rows["a"].subject == "fixed"
        assert rows["a"].parser_version == "v2"
        assert rows["a"].available_at == fetched
        assert rows["a"].first_seen_at == fetched
        assert rows["a"].attachment_status == "pending"
        assert rows["dropped-by-v1"].available_at == fetched

    def test_attachment_queue(self, engine: Engine, store: RawStore) -> None:
        doc = save_doc(engine, store)
        with engine.begin() as conn:
            repo.insert_announcements(
                conn,
                [make_record("a"), make_record("b")],
                raw_doc_id=doc,
                parser_version="v1",
                mode="live",
                fetched_at=T0,
                now=T0,
            )
            pending = repo.pending_attachments(conn, limit=10, max_attempts=2)
            assert len(pending) == 2
            for _ in range(2):
                repo.mark_attachment(conn, pending[0].id, status="failed")
            assert len(repo.pending_attachments(conn, limit=10, max_attempts=2)) == 1


class TestEod:
    def test_upsert_keeps_first_available_at(self, engine: Engine, store: RawStore) -> None:
        doc = save_doc(engine, store)
        rec = EodRecord(
            date(2026, 9, 25),
            "EXAMPLE",
            "EQ",
            100,
            101,
            110,
            99,
            108,
            108.2,
            105,
            1000,
            1.0,
            10,
            400,
            40.0,
        )
        with engine.begin() as conn:
            repo.upsert_eod(conn, [rec], raw_doc_id=doc, parser_version="v1", available_at=T0)
            repo.upsert_eod(
                conn,
                [replace(rec, close=109.0)],
                raw_doc_id=doc,
                parser_version="v2",
                available_at=T0 + timedelta(days=1),
            )
            row = conn.execute(select(eod_prices)).one()
            assert repo.has_rows_for_date(conn, eod_prices, "trade_date", date(2026, 9, 25))
        assert row.close == 109.0
        assert row.parser_version == "v2"
        assert row.available_at == T0


class TestPointInTime:
    def test_future_rows_are_invisible(self, engine: Engine, store: RawStore) -> None:
        doc = save_doc(engine, store)
        with engine.begin() as conn:
            repo.insert_announcements(
                conn,
                [make_record("early")],
                raw_doc_id=doc,
                parser_version="v1",
                mode="live",
                fetched_at=T0,
                now=T0,
            )
            repo.insert_announcements(
                conn,
                [make_record("late", event_ts=T0 + timedelta(hours=1))],
                raw_doc_id=doc,
                parser_version="v1",
                mode="live",
                fetched_at=T0 + timedelta(hours=1),
                now=T0 + timedelta(hours=1),
            )
            view = AsOf(conn, T0 + timedelta(minutes=30))
            seen = [r.source_ann_id for r in view.announcements_since(T0 - timedelta(days=1))]
            assert seen == ["early"]
            view.advance(T0 + timedelta(hours=2))
            seen = [r.source_ann_id for r in view.announcements_since(T0 - timedelta(days=1))]
            assert seen == ["early", "late"]
            with pytest.raises(ValueError, match="backwards"):
                view.advance(T0)

    def test_rejects_naive_clock(self, engine: Engine) -> None:
        with engine.begin() as conn, pytest.raises(ValueError, match="naive"):
            AsOf(conn, datetime(2026, 9, 25))


def test_raw_documents_are_recorded_once(engine: Engine, store: RawStore) -> None:
    save_doc(engine, store, b"same")
    save_doc(engine, store, b"same")
    with engine.begin() as conn:
        assert len(conn.execute(select(raw_documents)).all()) == 1
