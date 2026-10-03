"""NSE corporate actions: ``www.nseindia.com/api/corporates-corporateActions``.

Verified 2026-10-03 with ``index=equities&from_date=DD-MM-YYYY&to_date=...``
(one-year ranges, 1,955-2,706 rows a year, 2019-2026): a JSON list with
``symbol, series, isin, comp, subject, exDate, recDate, faceVal, bcStartDate,
bcEndDate, ndStartDate, ndEndDate, caBroadcastDate, ind``.

Two facts the rest of the system depends on:

- ``symbol`` and ``comp`` are **as of the fetch**, like the announcements
  API: LTI's 2019 dividend is reported under LTM, Tata Motors' 2023 dividend
  under TMPV. Translate with ``gats.refdata.symbols`` before joining prices.
- The bhavcopy's ``PREV_CLOSE`` is **not** adjusted on ex-dates (10/10 real
  splits and bonuses, Sep 2025), so returns need the share multipliers
  parsed here.

``subject`` is free text. The parser recognises the patterns seen in
2019-2026 data; anything that changes the share count or distributes value
but cannot be turned into a multiplier (rights, demergers, bonus preference
shares) is marked ``needs_review`` so studies can exclude those days.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date

from gats.sources._util import clean_str, looks_like_html, pick, preview, to_float
from gats.sources.models import ParseResult, PayloadError
from gats.timeutil import parse_date

SOURCE = "NSE"
KIND = "nse_corp_actions"
PARSER_VERSION = "nse-ca-v1"

_AMOUNT = r"r[se]\.?\s*([\d.]+)"
_BONUS = re.compile(r"\bbonus\s*[-:]?\s*(\d+)\s*:\s*(\d+)", re.IGNORECASE)
# NSE spells it both ways ("Scheme Of Arangement- Bonus - 1 Debenture ...").
_SCHEME = re.compile(r"scheme\s+of\s+arr?angement|demerger|capital\s+reduction", re.IGNORECASE)
# A "bonus" of something other than equity shares pays value out instead of
# splitting the share; no multiplier describes it.
_NON_EQUITY_BONUS = ("ncrps", "preference", "debenture")
_FROM_TO = re.compile(rf"from\s+{_AMOUNT}.*?\bto\s+{_AMOUNT}", re.IGNORECASE)
_DIVIDEND_AMOUNT = re.compile(rf"dividend\s*-?\s*{_AMOUNT}", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class CorporateActionRecord:
    reported_symbol: str  # as of the fetch, see module docstring
    series: str | None
    isin: str | None
    company: str | None
    subject: str
    ex_date: date
    record_date: date | None
    face_value: float | None
    kind: str  # BONUS, SPLIT, CONSOLIDATION, DIVIDEND, RIGHTS, DEMERGER, BUYBACK, OTHER
    # Shares held after the ex-date per share held before (2.0 for a 1:1
    # bonus, 5.0 for a split from Rs 10 to Rs 2). None: no change in count.
    share_multiplier: float | None
    cash_per_share: float | None  # dividends, rupees
    needs_review: bool  # changes value per share in a way no multiplier captures


def classify(subject: str) -> tuple[str, float | None, float | None, bool]:
    """``(kind, share_multiplier, cash_per_share, needs_review)`` for a subject."""
    text = " ".join(subject.split())
    low = text.lower()
    multiplier = 1.0
    kinds: list[str] = []
    review = False

    if "bonus" in low and any(word in low for word in _NON_EQUITY_BONUS):
        kinds.append("DEMERGER")
        review = True
    else:
        for match in _BONUS.finditer(text):
            new, held = int(match.group(1)), int(match.group(2))
            if held > 0:
                multiplier *= (new + held) / held
                kinds.append("BONUS")
    if ("split" in low or "sub-division" in low or "consolidation" in low) and (
        found := _FROM_TO.search(text)
    ):
        old_fv, new_fv = float(found.group(1)), float(found.group(2))
        if old_fv > 0 and new_fv > 0:
            multiplier *= old_fv / new_fv
            kinds.append("SPLIT" if new_fv < old_fv else "CONSOLIDATION")
    elif "split" in low or "sub-division" in low or "consolidation" in low:
        review = True  # a share-count change we could not read

    if "rights" in low:
        kinds.append("RIGHTS")
        review = True  # adjustment needs the rights price and market price
    if _SCHEME.search(text) and "DEMERGER" not in kinds:
        kinds.append("DEMERGER")
        review = True
    cash = None
    amounts = [float(a) for a in _DIVIDEND_AMOUNT.findall(text)]
    if "dividend" in low:
        kinds.append("DIVIDEND")
        cash = sum(amounts) if amounts else None
    if "buy back" in low or "buyback" in low:
        kinds.append("BUYBACK")

    kind = kinds[0] if kinds else "OTHER"
    return kind, (multiplier if multiplier != 1.0 else None), cash, review


def request_params(start: date, end: date) -> dict[str, str]:
    return {
        "index": "equities",
        "from_date": start.strftime("%d-%m-%Y"),
        "to_date": end.strftime("%d-%m-%Y"),
    }


def request_headers(referer: str) -> dict[str, str]:
    return {"Accept": "application/json, text/plain, */*", "Referer": referer}


def parse_corporate_actions(payload: bytes) -> ParseResult[CorporateActionRecord]:
    if looks_like_html(payload):
        raise PayloadError(f"NSE corporate actions: got HTML: {preview(payload)}")
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise PayloadError(f"NSE corporate actions: not JSON ({exc}): {preview(payload)}") from exc
    if not isinstance(data, list):
        raise PayloadError(f"NSE corporate actions: not a JSON list: {preview(payload)}")
    result: ParseResult[CorporateActionRecord] = ParseResult(records=[])
    for index, row in enumerate(data):
        if not isinstance(row, dict):
            continue
        symbol = clean_str(pick(row, "symbol"))
        subject = clean_str(pick(row, "subject"))
        ex_date = parse_date(pick(row, "exDate"))
        if not symbol or not subject or ex_date is None:
            result.warnings.append(f"row {index}: missing symbol, subject or exDate")
            continue
        kind, multiplier, cash, review = classify(subject)
        result.records.append(
            CorporateActionRecord(
                reported_symbol=symbol.upper(),
                series=clean_str(pick(row, "series")),
                isin=clean_str(pick(row, "isin")),
                company=clean_str(pick(row, "comp")),
                subject=subject,
                ex_date=ex_date,
                record_date=parse_date(pick(row, "recDate")),
                face_value=to_float(pick(row, "faceVal")),
                kind=kind,
                share_multiplier=multiplier,
                cash_per_share=cash,
                needs_review=review,
            )
        )
    return result
