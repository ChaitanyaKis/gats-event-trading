"""Attachment text extraction (T4.1)."""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pypdf
import respx
from sqlalchemy import select

from gats.db import repo
from gats.db.schema import announcements, document_texts
from gats.extract.pdf_text import EXTRACTOR, extract_text
from gats.extract.texts import document_text, extract_pending
from gats.ingest import Services, fetch_attachments_for
from gats.sources.models import AnnouncementRecord

PDF = (Path(__file__).parent / "fixtures" / "real" / "order_win_attachment.pdf").read_bytes()
NOW = datetime(2026, 10, 3, tzinfo=UTC)


def squash(text: str) -> str:
    return " ".join(text.split())


def test_real_order_win_pdf() -> None:
    result = extract_text(PDF)
    assert result.error is None and not result.needs_ocr
    assert result.pages == 2 and result.chars > 2000
    assert "received purchase orders aggregating to Rs. 60 crore" in squash(result.text)


def test_pdf_inside_a_zip() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("covering_letter.pdf", PDF)
        archive.writestr("readme.txt", b"not a pdf")
    result = extract_text(buffer.getvalue())
    assert result.parts == ["covering_letter.pdf"] and result.pages == 2
    assert "Rs. 60 crore" in squash(result.text)


def _blank_pdf(encrypt: bool = False) -> bytes:
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=595, height=842)
    if encrypt:
        writer.encrypt(user_password="", owner_password="owner")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def test_image_only_pdf_needs_ocr() -> None:
    result = extract_text(_blank_pdf())
    assert result.error is None and result.pages == 1 and result.needs_ocr


def test_encrypted_with_empty_password_is_read() -> None:
    result = extract_text(_blank_pdf(encrypt=True))
    assert result.error is None and result.pages == 1


def test_unusable_payloads_report_errors() -> None:
    assert extract_text(b"%PDF-1.7 truncated garbage").error is not None
    assert extract_text(b"<!DOCTYPE html><html>blocked</html>").error == "HTML page, not a document"
    assert extract_text(b"\x00\x01binary").error == "unsupported file type"
    empty_zip = io.BytesIO()
    zipfile.ZipFile(empty_zip, "w").close()
    assert extract_text(empty_zip.getvalue()).error == "zip holds no PDF"


@respx.mock
async def test_fetch_bypasses_policy_then_extract(svc: Services) -> None:
    url = "https://nsearchives.nseindia.com/corporate/GOLDIAM_08092026_order.pdf"
    record = AnnouncementRecord(
        source="NSE",
        source_ann_id="1",
        symbol="GOLDIAM",
        scrip_code=None,
        isin=None,
        company_name="Goldiam",
        category="Bagging/Receiving of orders/contracts",
        subcategory=None,
        subject=None,
        details=None,
        attachment_url=url,
        exch_submitted_ts=None,
        exch_disseminated_ts=NOW,
        event_ts=NOW,
    )
    with svc.engine.begin() as conn:
        doc = repo.save_raw(
            conn,
            svc.store,
            b"page",
            kind="t",
            source="NSE",
            url="u",
            content_type=None,
            fetched_at=NOW,
        )
        repo.insert_announcements(
            conn,
            [record],
            raw_doc_id=doc,
            parser_version="v",
            mode="backfill",
            fetched_at=NOW,
            now=NOW,
        )
        ann_id = int(conn.execute(select(announcements.c.id)).scalar_one())
        # The storage policy had skipped it; a study now needs it.
        repo.mark_attachment(conn, ann_id, status="skipped", attempted=False)
    respx.get(svc.settings.nse_home_url).mock(return_value=httpx.Response(200))
    respx.get(url).mock(return_value=httpx.Response(200, content=PDF))
    outcome = await fetch_attachments_for(svc, [ann_id], job="t", limit=10)
    assert outcome.n_new == 1 and outcome.meta == {"done": 1}

    with svc.engine.begin() as conn:
        first = extract_pending(conn, svc.store, NOW)
        again = extract_pending(conn, svc.store, NOW)
        doc_id = conn.execute(select(announcements.c.attachment_doc_id)).scalar_one()
        stored = conn.execute(select(document_texts)).one()
        text = document_text(conn, doc_id)
    assert (first.documents, first.with_text, again.documents) == (1, 1, 0)
    assert stored.extractor == EXTRACTOR and stored.pages == 2 and not stored.needs_ocr
    assert text is not None and "Rs. 60 crore" in squash(text)
