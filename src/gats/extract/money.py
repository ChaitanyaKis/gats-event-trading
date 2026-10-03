"""Amounts as Indian filings write them: "Rs. 8,75,40,000/-", "₹ 76.06 crore",
"INR 178.61 Lakhs", "214.52 Cr.", "US$9.78 mn".

Rules (from 1,133 distinct phrasings in 363 real order-win attachments):

- Indian (8,75,40,000) and Western (23,397,835.32) digit grouping both occur;
  commas are dropped, the decimal point kept. PDF text may put a space after
  a grouping comma ("Rs. 1, 303 Crores"): the groups join only when they run
  into a unit, so a list ("Rs. 100, 200 and 300 crore") stays apart.
- A range gives its lower bound ("Rs.10-17 crores" is 10 crore): the
  conservative reading of an order's size.
- Units: crore (cr, crores) 10^7, lakh (lakhs, lac, lacs) 10^5, million (mn)
  10^6, billion (bn) 10^9, thousand 10^3.
- Currency from the marker (₹, Rs, INR, Rupees, USD, US$, $, EUR, €, GBP, £,
  AED). Without a marker, crore and lakh imply rupees (Indian units);
  million and billion stay currency-unknown.
- Only rupee amounts get ``amount_inr``. Converting other currencies needs a
  dated FX rate from a verified source, which this module does not invent.
- Amounts in words, "(Rupees Two Hundred Seventeen Crore Fifty Six Lakh
  only)", restate the figure in about a quarter of the texts (94 of 363) and
  are what counts legally. They rescue figures the PDF garbled ("Rs. 6
  60.79/-") or printed without a unit ("Rs. 217.56"). A word that is not a
  number word (the real typo "Thifty") makes the whole phrase unreadable:
  never guessed.
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
_UNIT_WORD = r"(?:crores?|cror|cr|lakhs?|lakh\(s\)|lacs?|millions?|mn|mio|billions?|bn|thousand)\b"
_UNIT = rf"(?P<unit>{_UNIT_WORD})"
_SPLIT_GROUP = rf"\s(?=\d{{2,3}}(?:,\d{{2,3}})*(?:\.\d+)?\s*{_UNIT_WORD})"
_NUMBER = rf"(?P<num>\d(?:\d|,(?:{_SPLIT_GROUP})?)*(?:\.\d+)?)"
_RANGE = r"(?:\s*(?:-|–|to)\s*\d[\d,]*(?:\.\d+)?)?"  # upper bound, ignored
# Two anchored patterns: a currency marker may not start inside a word
# ("orders 2 units" is not "rs 2"), and a bare number with a unit may not
# start inside another token (an ISIN, a decimal).
WITH_CURRENCY_RE = re.compile(
    rf"(?<![A-Za-z]){_CURRENCY}\s*{_NUMBER}{_RANGE}\s*(?:/-)?\s*(?:{_UNIT})?\.?", re.IGNORECASE
)
UNIT_ONLY_RE = re.compile(rf"(?<![\w.,]){_NUMBER}{_RANGE}\s*{_UNIT}\.?", re.IGNORECASE)
IN_WORDS_RE = re.compile(
    r"(?<![A-Za-z])(?:rupees|rs\.?|inr)\s+(?P<words>[a-z][a-z\s&,-]{0,250}?)\s*\bonly\b",
    re.IGNORECASE,
)

_ONES = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
    "eighteen", "nineteen",
)  # fmt: skip
_TENS = ("twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")
_SMALL = {word: n for n, word in enumerate(_ONES)} | {
    word: 10 * n for n, word in enumerate(_TENS, start=2)
}
_SCALES = {
    "thousand": 1e3,
    "lakh": 1e5,
    "lakhs": 1e5,
    "lac": 1e5,
    "lacs": 1e5,
    "million": 1e6,
    "millions": 1e6,
    "crore": 1e7,
    "crores": 1e7,
    "billion": 1e9,
    "billions": 1e9,
}
_FILLERS = frozenset({"and", "rupees", "rupee", "only"})
_PAISE = ("paise", "paisa")
_VOCAB = frozenset(_SMALL) | frozenset(_SCALES) | _FILLERS | {"hundred", *_PAISE}


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
    cleaned = re.sub(r"[,\s]", "", raw).rstrip(".")
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


def _tokens(words: str) -> list[str]:
    raw = re.sub(r"[-&,]", " ", words.lower()).split()
    tokens: list[str] = []
    i = 0
    while i < len(raw):
        token = raw[i]
        if token not in _VOCAB and i + 1 < len(raw) and token + raw[i + 1] in _VOCAB:
            token += raw[i + 1]  # a word the PDF split ("Hundre d")
            i += 1
        tokens.append(token)
        i += 1
    return tokens


def _integer(tokens: list[str]) -> float | None:
    """Indian or Western number words -> value. A larger scale after a smaller
    one multiplies everything before it ("one lakh twenty thousand crore")."""
    total = current = largest = 0.0
    for token in tokens:
        if token in _FILLERS:
            continue
        if token in _SMALL:
            current += _SMALL[token]
        elif token == "hundred":
            current = (current or 1) * 100
        elif token in _SCALES:
            scale = _SCALES[token]
            if current == 0 and total == 0:
                return None
            if scale > largest > 0:
                total = (total + current) * scale
            else:
                total += current * scale
            current = 0
            largest = max(largest, scale)
        else:
            return None
    total += current
    return total or None


def words_to_rupees(words: str) -> float | None:
    """ "Two Hundred Seventeen Crore Fifty Six Lakh" -> 2_175_600_000.0, with
    paise either way round ("and Paise Ninety", "and Eighty-Seven paise").
    None unless every word is a number word."""
    tokens = _tokens(words)
    paise = 0.0
    marker = next((t for t in tokens if t in _PAISE), None)
    if marker is not None:
        at = tokens.index(marker)
        after = [t for t in tokens[at + 1 :] if t not in _FILLERS]
        if after:  # "... and Paise Ninety"
            cents = _integer(after)
            tokens = tokens[:at]
        else:  # "... and Eighty-Seven paise"
            start = at
            while start > 0 and tokens[start - 1] in _SMALL:
                start -= 1
            if start == at or start == 0 or tokens[start - 1] != "and":
                return None
            cents = _integer(tokens[start:at])
            tokens = tokens[: start - 1]
        if cents is None or cents >= 100:
            return None
        paise = cents / 100
    rupees = _integer(tokens)
    return None if rupees is None else rupees + paise


def find_amounts(text: str) -> list[Money]:
    """Every amount in ``text``, figures and words, in order."""
    found = [(match.start(), _to_money(match)) for match in _matches(text)]
    for match in IN_WORDS_RE.finditer(text):
        value = words_to_rupees(match.group("words"))
        if value is not None:
            found.append((match.start(), Money(value, "INR", match.group(0).strip())))
    found.sort(key=lambda pair: pair[0])
    return [money for _, money in found if money is not None]


def parse_amount(text: str) -> Money | None:
    """The first amount in ``text``, normalised; None if there is none."""
    found = find_amounts(text)
    return found[0] if found else None
