"""Store extracted attachment text (``document_texts``) and measure coverage."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, and_, func, select

from gats.db import repo
from gats.db.schema import (
    announcement_event_types,
    announcements,
    document_texts,
    raw_documents,
)
from gats.extract.pdf_text import EXTRACTOR, EXTRACTOR_VERSION, extract_text
from gats.rawstore import RawStore

_BATCH = 200


@dataclass
class ExtractStats:
    documents: int = 0
    with_text: int = 0
    needs_ocr: int = 0
    errors: int = 0
    error_samples: list[str] = field(default_factory=list)


def extract_pending(
    conn: Connection, store: RawStore, now: datetime, *, limit: int = 10**7
) -> ExtractStats:
    """Extract every attachment not yet extracted by this extractor version."""
    stats = ExtractStats()
    t = document_texts
    while stats.documents < limit:
        docs = (
            conn.execute(
                select(raw_documents.c.doc_id)
                .select_from(
                    raw_documents.outerjoin(
                        t,
                        and_(
                            t.c.doc_id == raw_documents.c.doc_id,
                            t.c.extractor == EXTRACTOR,
                            t.c.extractor_version == EXTRACTOR_VERSION,
                        ),
                    )
                )
                .where(raw_documents.c.kind == "attachment", t.c.doc_id.is_(None))
                .limit(min(_BATCH, limit - stats.documents))
            )
            .scalars()
            .all()
        )
        if not docs:
            break
        rows = []
        for doc_id in docs:
            result = extract_text(store.get(doc_id))
            stats.documents += 1
            if result.error:
                stats.errors += 1
                if len(stats.error_samples) < 5:
                    stats.error_samples.append(f"{doc_id[:12]}: {result.error}")
            elif result.needs_ocr:
                stats.needs_ocr += 1
            else:
                stats.with_text += 1
            rows.append(
                {
                    "doc_id": doc_id,
                    "extractor": EXTRACTOR,
                    "extractor_version": EXTRACTOR_VERSION,
                    "pages": result.pages,
                    "chars": result.chars,
                    "needs_ocr": result.needs_ocr,
                    "error": result.error,
                    "text": result.text,
                    "extracted_at": now,
                }
            )
        repo.insert_ignore(conn, t, rows, ["doc_id", "extractor", "extractor_version"])
    return stats


@dataclass
class TextCoverage:
    event_type: str
    filings: int
    downloaded: int
    with_text: int
    needs_ocr: int
    errors: int
    by_status: dict[str, int]

    @property
    def share(self) -> float:
        """Acceptance metric (T4.1): downloaded in-scope PDFs that yield text."""
        return self.with_text / self.downloaded if self.downloaded else 0.0


def text_coverage(conn: Connection, event_type: str, taxonomy_version: str) -> TextCoverage:
    a, et, t = announcements, announcement_event_types, document_texts
    base = a.join(
        et,
        and_(
            et.c.announcement_id == a.c.id,
            et.c.taxonomy_version == taxonomy_version,
            et.c.event_type == event_type,
        ),
    )
    by_status = {
        str(status): int(n)
        for status, n in conn.execute(
            select(a.c.attachment_status, func.count())
            .select_from(base)
            .where(a.c.attachment_url.is_not(None))
            .group_by(a.c.attachment_status)
        )
    }
    texts: Any = conn.execute(
        select(t.c.needs_ocr, t.c.error).select_from(
            base.join(
                t,
                and_(
                    t.c.doc_id == a.c.attachment_doc_id,
                    t.c.extractor == EXTRACTOR,
                    t.c.extractor_version == EXTRACTOR_VERSION,
                ),
            )
        )
    ).all()
    return TextCoverage(
        event_type=event_type,
        filings=sum(by_status.values()),
        downloaded=by_status.get("done", 0),
        with_text=sum(1 for ocr, err in texts if err is None and not ocr),
        needs_ocr=sum(1 for ocr, err in texts if err is None and ocr),
        errors=sum(1 for _ocr, err in texts if err is not None),
        by_status=by_status,
    )


def document_text(conn: Connection, doc_id: str) -> str | None:
    """The newest extraction of a document's text, if any."""
    row = conn.execute(
        select(document_texts.c.text)
        .where(document_texts.c.doc_id == doc_id, document_texts.c.error.is_(None))
        .order_by(document_texts.c.extracted_at.desc())
        .limit(1)
    ).first()
    return None if row is None else str(row.text)
