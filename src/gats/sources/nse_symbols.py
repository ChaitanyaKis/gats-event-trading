"""NSE symbol changes: ``nsearchives.nseindia.com/content/equities/symbolchange.csv``.

Verified 2026-10-02: a cumulative, **headerless** CSV of every symbol change
since 1999 (1,065 rows): company name, old symbol, new symbol, effective
date (``DD-MON-YYYY``). The effective date is the first session under the
new symbol: ``sec_bhavdata_full`` lists ZOMATO on 08-Apr-2025 and ETERNAL
on 09-Apr-2025, the row's date. Some rows (debt, old MF units) have an empty
company name.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date

from gats.sources._util import clean_str, decode_text, looks_like_html, preview
from gats.sources.models import ParseResult, PayloadError
from gats.timeutil import parse_date

SOURCE = "NSE"
KIND = "nse_symbol_changes"
PARSER_VERSION = "nse-symbolchange-v1"


@dataclass(frozen=True, slots=True)
class SymbolChangeRecord:
    old_symbol: str
    new_symbol: str
    effective_date: date
    company_name: str | None


def parse_symbol_changes(payload: bytes) -> ParseResult[SymbolChangeRecord]:
    if looks_like_html(payload):
        raise PayloadError(f"symbol change file: got HTML instead of CSV: {preview(payload)}")
    rows = [r for r in csv.reader(io.StringIO(decode_text(payload))) if any(c.strip() for c in r)]
    result: ParseResult[SymbolChangeRecord] = ParseResult(records=[])
    for index, row in enumerate(rows):
        if len(row) != 4:
            result.warnings.append(f"row {index}: expected 4 columns, got {len(row)}")
            continue
        name, old, new, raw_date = (clean_str(c) for c in row)
        effective = parse_date(raw_date)
        if effective is None:
            # A header row, should NSE ever add one, lands here too.
            result.warnings.append(f"row {index}: unparseable date {raw_date!r}")
            continue
        if not old or not new:
            result.warnings.append(f"row {index}: missing symbol")
            continue
        result.records.append(
            SymbolChangeRecord(
                old_symbol=old.upper(),
                new_symbol=new.upper(),
                effective_date=effective,
                company_name=name,
            )
        )
    if rows and not result.records:
        raise PayloadError(f"symbol change file: no parseable rows: {preview(payload)}")
    return result
