"""Text from filing attachments (PDFs, and PDFs inside NSE's zip files).

The LLM and the rules extract facts from this text, so extraction is
versioned like a parser: ``EXTRACTOR`` names the library and its version,
and a new version re-extracts into new rows instead of overwriting.

Scanned PDFs carry images, not text. They are flagged ``needs_ocr`` (fewer
than ``MIN_CHARS_PER_PAGE`` characters per page) rather than silently stored
as empty; OCR is out of scope unless the human asks for it (T4.1).
"""

from __future__ import annotations

import io
import logging
import zipfile
from dataclasses import dataclass, field

import pypdf

EXTRACTOR = f"pypdf-{pypdf.__version__}"
EXTRACTOR_VERSION = "1"
MIN_CHARS_PER_PAGE = 50
MAX_PAGES = 200  # annual reports run to hundreds of pages; facts are up front
PAGE_BREAK = "\f"

# pypdf logs a warning for every malformed object; exchange PDFs have many.
logging.getLogger("pypdf").setLevel(logging.ERROR)


@dataclass
class Extracted:
    pages: int = 0
    text: str = ""
    error: str | None = None
    parts: list[str] = field(default_factory=list)  # file names inside a zip

    @property
    def chars(self) -> int:
        return len(self.text)

    @property
    def needs_ocr(self) -> bool:
        return (
            self.error is None and self.pages > 0 and self.chars < MIN_CHARS_PER_PAGE * self.pages
        )


def _pdf(payload: bytes) -> Extracted:
    try:
        reader = pypdf.PdfReader(io.BytesIO(payload), strict=False)
        if reader.is_encrypted:
            # Most "encrypted" filings use an empty user password (print/copy
            # restrictions only).
            reader.decrypt("")
        pages = []
        for page in reader.pages[:MAX_PAGES]:
            pages.append(page.extract_text() or "")
        return Extracted(pages=len(reader.pages), text=PAGE_BREAK.join(pages))
    except Exception as exc:  # pypdf raises many types on broken files
        return Extracted(error=f"{type(exc).__name__}: {exc}"[:500])


def extract_text(payload: bytes) -> Extracted:
    """Text of a PDF, or of every PDF in a zip, in file order."""
    if payload[:5] == b"%PDF-":
        return _pdf(payload)
    if payload[:2] == b"PK":
        try:
            archive = zipfile.ZipFile(io.BytesIO(payload))
        except zipfile.BadZipFile as exc:
            return Extracted(error=f"BadZipFile: {exc}")
        combined = Extracted()
        for name in sorted(archive.namelist()):
            if not name.lower().endswith(".pdf"):
                continue
            part = _pdf(archive.read(name))
            combined.parts.append(name)
            if part.error is None:
                combined.pages += part.pages
                combined.text = (combined.text + PAGE_BREAK + part.text).strip(PAGE_BREAK)
        if not combined.parts:
            combined.error = "zip holds no PDF"
        elif combined.pages == 0:
            combined.error = "no readable PDF in zip"
        return combined
    head = payload[:200].lstrip().lower()
    if head.startswith((b"<!doctype html", b"<html")):
        return Extracted(error="HTML page, not a document")
    return Extracted(error="unsupported file type")
