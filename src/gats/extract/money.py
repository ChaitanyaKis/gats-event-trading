"""Amounts as Indian filings write them: "Rs. 8,75,40,000/-", "₹ 76.06 crore",
"INR 178.61 Lakhs", "214.52 Cr.", "US$9.78 mn".

Rules (from 1,133 distinct phrasings in 363 real order-win attachments):

- Indian (8,75,40,000) and Western (23,397,835.32) digit grouping both occur;
  commas are dropped, the decimal point kept.
- Units: crore (cr, crores) 10^7, lakh (lakhs, lac, lacs) 10^5, million (mn)
  10^6, billion (bn) 10^9, thousand 10^3.
- Currency from the marker (₹, Rs, INR, Rupees, USD, US$, $, EUR, €, GBP, £,
  AED). Without a marker, crore and lakh imply rupees (Indian units);
  million and billion stay currency-unknown.
- Only rupee amounts get ``amount_inr``. Converting other currencies needs a
  dated FX rate from a verified source, which this module does not invent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

UNITS: dict[str, float] = {
    "crore": 1e7,
    "crores": 1e7,
    "cr": 1e7,
    "cror": 1e7,
    "lakh": 1e5,
    "lakhs": 1e5,
    "lac": 1e5,
    "lacs": 1e5,
    "lakh(s)": 1e5,
    "million": 1e6,
    "millions": 1e6,
    "mn": 1e6,
    "mio": 1e6,
    "billion": 1e9,
    "billions": 1e9,
    "bn": 1e9,
    "thousand": 1e3,
}
INDIAN_UNITS = frozenset(
    {"crore", "crores", "cr", "cror", "lakh", "lakhs", "lac", "lacs", "lakh(s)"}
)

_CURRENCY = r"(?P<cur>₹|rs\.?|inr|rupees|usd|us\s?\$|\$|eur|€|gbp|£|aed)"
_NUMBER = r"(?P<num>\d[\d,]*(?:\.\d+)?)"
_UNIT = r"(?P<unit>crores?|cror|cr|lakhs?|lakh\(s\)|lacs?|millions?|mn|mio|billions?|bn|thousand)\b"
# Two anchored patterns: a currency marker may not start inside a word
# ("orders 2 units" is not "rs 2"), and a bare number with a unit may not
# start inside another token (an ISIN, a decimal).
WITH_CURRENCY_RE = re.compile(
    rf"(?<![A-Za-z]){_CURRENCY}\s*{_NUMBER}\s*(?:/-)?\s*(?:{_UNIT})?\.?", re.IGNORECASE
)
UNIT_ONLY_RE = re.compile(rf"(?<![\w.,]){_NUMBER}\s*{_UNIT}\.?", re.IGNORECASE)


_CURRENCY_CODES = {
    "₹": "INR",
    "rs": "INR",
    "rs.": "INR",
    "inr": "INR",
    "rupees": "INR",
    "usd": "USD",
    "us$": "USD",
    "us $": "USD",
    "$": "USD",
    "eur": "EUR",
    "€": "EUR",
    "gbp": "GBP",
    "£": "GBP",
    "aed": "AED",
}


@dataclass(frozen=True, slots=True)
class Money:
    amount: float  # in units of ``currency`` (unit multiplier applied)
    currency: str | None  # ISO code, or None when the text does not say
    text: str  # verbatim match

    @property
    def amount_inr(self) -> float | None:
        return self.amount if self.currency == "INR" else None


def _number(raw: str) -> float | None:
    cleaned = raw.replace(",", "").rstrip(".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _matches(text: str) -> list[re.Match[str]]:
    found = list(WITH_CURRENCY_RE.finditer(text)) + list(UNIT_ONLY_RE.finditer(text))
    found.sort(key=lambda m: m.start())
    kept: list[re.Match[str]] = []
    for match in found:  # a unit-only match inside a currency match is the same amount
        if kept and match.start() < kept[-1].end():
            continue
        kept.append(match)
    return kept


def _to_money(match: re.Match[str]) -> Money | None:
    groups = match.groupdict()
    value = _number(groups["num"])
    if value is None:
        return None
    unit = (groups.get("unit") or "").lower()
    currency_raw = re.sub(r"\s+", " ", (groups.get("cur") or "").lower())
    currency = _CURRENCY_CODES.get(currency_raw) if currency_raw else None
    if currency is None and unit in INDIAN_UNITS:
        currency = "INR"
    return Money(value * UNITS.get(unit, 1.0), currency, match.group(0).strip())


def find_amounts(text: str) -> list[Money]:
    """Every amount in ``text``, in order."""
    return [m for match in _matches(text) if (m := _to_money(match)) is not None]


def parse_amount(text: str) -> Money | None:
    """The first amount in ``text``, normalised; None if there is none."""
    found = find_amounts(text)
    return found[0] if found else None
