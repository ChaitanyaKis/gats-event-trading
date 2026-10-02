"""BSE list of scrips: scrip code -> ISIN, ticker, group, status.

Endpoint verified against the live API on 2026-10-02:
``api.bseindia.com/BseIndiaAPI/api/ListofScripData/w`` with
``segment=Equity`` and an empty ``status`` returns every equity scrip ever
listed (10,918 rows: Active 5,069, Delisted 4,617, Suspended 1,229, ``N``
3). The API answers bare clients with an Akamai 403; it needs browser-like
headers and the BSE homepage cookies (``PoliteClient`` provides both).

``ISIN_NUMBER`` is ``NA`` or empty for ~2,300 rows (mostly long-delisted
scrips); those map to ``isin=None``. The list is current-only: BSE does not
publish its history, so versions are built from daily snapshots
(see ``gats.refdata``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from gats.sources._util import clean_str, looks_like_html, pick, preview, to_float
from gats.sources.models import ParseResult, PayloadError

SOURCE = "BSE"
KIND = "bse_scrips"
PARSER_VERSION = "bse-scrips-v1"

ISIN_RE = re.compile(r"^IN[A-Z0-9]{9}[0-9]$")

KNOWN_FIELDS = frozenset(
    {
        "scrip_cd",
        "scrip_name",
        "status",
        "group",
        "face_value",
        "isin_number",
        "industry",
        "scrip_id",
        "segment",
        "issuer_name",
        # Not stored: a page link and a current-only market cap whose units
        # BSE does not document.
        "nsurl",
        "mktcap",
    }
)


@dataclass(frozen=True, slots=True)
class BseScripRecord:
    scrip_code: str
    symbol: str | None  # BSE "scrip_id", e.g. ABB
    name: str | None
    issuer_name: str | None
    isin: str | None
    status: str | None  # Active | Suspended | Delisted
    group: str | None  # A, B, T, X, XT, Z, M, ...
    face_value: float | None
    segment: str | None
    industry: str | None


def request_params() -> dict[str, str]:
    """All statuses: delisted scrips are needed to resolve old filings."""
    return {"Group": "", "Scripcode": "", "industry": "", "segment": "Equity", "status": ""}


def request_headers(referer: str) -> dict[str, str]:
    return {
        "Accept": "application/json, text/plain, */*",
        "Referer": referer,
        "Origin": referer.rstrip("/"),
    }


def normalise_isin(value: Any) -> str | None:
    text = clean_str(value)
    if text is None:
        return None
    text = text.upper()
    return text if ISIN_RE.match(text) else None


def parse_scrips(payload: bytes) -> ParseResult[BseScripRecord]:
    if looks_like_html(payload):
        raise PayloadError(f"BSE scrip list returned HTML (blocked?): {preview(payload)}")
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise PayloadError(f"BSE scrip list is not JSON ({exc}): {preview(payload)}") from exc
    if not isinstance(data, list):
        raise PayloadError(f"BSE scrip list is not a JSON list: {preview(payload)}")

    result: ParseResult[BseScripRecord] = ParseResult(records=[])
    unknown: set[str] = set()
    seen: set[str] = set()
    for index, row in enumerate(data):
        if not isinstance(row, dict):
            continue
        unknown.update(k.lower() for k in row if k.lower() not in KNOWN_FIELDS)
        code = clean_str(pick(row, "SCRIP_CD"))
        if not code:
            result.warnings.append(f"row {index}: missing SCRIP_CD, skipped")
            continue
        if code in seen:
            result.warnings.append(f"row {index}: duplicate SCRIP_CD {code}, skipped")
            continue
        seen.add(code)
        raw_isin = clean_str(pick(row, "ISIN_NUMBER"))
        isin = normalise_isin(raw_isin)
        if raw_isin and isin is None and raw_isin.upper() != "NA":
            result.warnings.append(f"row {index} ({code}): invalid ISIN {raw_isin!r}")
        result.records.append(
            BseScripRecord(
                scrip_code=code,
                symbol=clean_str(pick(row, "scrip_id")),
                name=clean_str(pick(row, "Scrip_Name")),
                issuer_name=clean_str(pick(row, "Issuer_Name")),
                isin=isin,
                status=clean_str(pick(row, "Status")),
                group=clean_str(pick(row, "GROUP")),
                face_value=to_float(pick(row, "FACE_VALUE")),
                segment=clean_str(pick(row, "Segment")),
                industry=clean_str(pick(row, "INDUSTRY")),
            )
        )
    result.meta["unknown_fields"] = sorted(unknown)
    return result
