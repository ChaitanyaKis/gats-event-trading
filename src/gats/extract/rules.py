"""Rules baseline for ORDER_WIN facts (T4.3).

Two layouts cover most filings (362 real order-win texts, 2026-09):

- **SEBI's disclosure annexure** (about 2/3): numbered fields such as "Name
  of the entity awarding the order(s)/contract(s)", "Whether domestic or
  international", "Time period by which the order(s)/contract(s) is to be
  executed", "Broad consideration or size of the order(s)/contract(s)". PDF
  text interleaves table cells, so a field's value is the text between its
  label and the next label, with label fragments removed.
- **A covering letter**: a sentence like "received purchase orders
  aggregating to Rs. 60 crore".

The result carries how it was found. Low confidence marks it ``unsure``, so
the cascade (T4.4) sends it to the LLM. Numbers always go through
:class:`OrderWin`, which recomputes them from the verbatim text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from gats.extract.money import Money, find_amounts
from gats.extract.schemas import MIN_ORDER_INR, OrderWin

# Bump on any rule change: stored extractions are keyed by it.
# The human-readable part of the stored version; the cascade appends a hash
# of the code, so every edit re-extracts anyway. Bump it for changes worth
# naming. v4: label priority, fallback candidates, a 1-lakh floor, amounts
# in words (v2 and v3 were development states, found only in a dev DB).
RULES_VERSION = "order-rules-v4"

# Field labels of the SEBI annexure, as PDF text renders them. Each field
# lists its alternatives best first: the canonical SEBI wording wins over
# looser phrases that also occur in prose ("Contract valued at ..." once
# matched "contract value" ahead of the real "Broad consideration or size").
_LABELS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "counterparty",
        (
            r"name of the (?:entity|party|customer|company|client)\s*(?:/\s*\w+\s*)?"
            r"(?:awarding|from whom|who has (?:awarded|placed)|to whom)",
        ),
    ),
    ("terms", (r"significant terms(?:\s+and\s+conditions)?",)),
    (
        "awarded_by",
        (
            r"whether (?:the )?order\s*\(\s*s\s*\)\s*/?\s*contract\s*\(\s*s\s*\)\s*"
            r"(?:have|has) been\s+awarded by",
        ),
    ),
    ("nature", (r"nature of (?:the )?order",)),
    ("dom_intl", (r"whether domestic or\s+internationa\s?l",)),
    ("period", (r"time period",)),
    (
        "size",
        (
            r"broad consideration or\s+size",
            r"\bsize of (?:the )?order",
            r"\bvalue of (?:the )?order\b",
            r"\border value\b",
            r"\bcontract value\b",
        ),
    ),
    ("promoter", (r"whether the\s+promoter",)),
    ("related", (r"related party\s+transaction", r"fall within related party")),
)
_LABEL_RES = [
    (name, [re.compile(p, re.IGNORECASE) for p in patterns]) for name, patterns in _LABELS
]


# Label text the PDF layout leaves inside values.
_FRAGMENTS = re.compile(
    r"(?:the\s+)?order\s*\(\s*s\s*\)\s*/?\s*contra\s*ct\s*\(\s*s\s*\)\s*(?:is to be executed|"
    r"awarded in brief|awarded,? in brief|have been awarded|;)?|in brief\s*;?|be provided|"
    r"is to be executed\s*;?|associated with the\s*;?|if any,?",
    re.IGNORECASE,
)
_ORDER_WORDS = re.compile(
    r"\b(order|contract|work order|letter of (?:award|acceptance|intent)|LoA|LoI|purchase order|"
    r"tender|bid)\w*",
    re.IGNORECASE,
)
_VALUE_WORDS = re.compile(
    r"aggregat|worth|valu|amount|consideration|size|cost|price", re.IGNORECASE
)
_NOT_ORDER_VALUE = re.compile(
    r"turnover|revenue|net worth|share capital|face value|paid[- ]up|market cap|"
    r"earnest money|\bemd\b|bank guarantee|security deposit|penalt",
    re.IGNORECASE,
)
_DURATION = re.compile(
    r"(\d+(?:\.\d+)?)\s*(months?|mths?|years?|yrs?|days?|weeks?)\b", re.IGNORECASE
)
_DATE = re.compile(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4})\b")
_EXPORT = re.compile(r"\binternational\b|\bforeign\b|\boverseas\b|\bexport", re.IGNORECASE)
_DOMESTIC = re.compile(r"\bdomestic\b", re.IGNORECASE)
# The answer to "Whether domestic or international" sits right after the
# label; the PDF may interleave it ("Whether domestic or Domestic internationa l").
_DOM_LABEL = re.compile(r"whether domestic\s+or\b", re.IGNORECASE)
_LABEL_WORD = re.compile(r"internationa\s?l\s*[;:]?|/\s*international", re.IGNORECASE)
_STRIP = " :;,.-\u2013\u2022"


@dataclass(frozen=True)
class RuleResult:
    order: OrderWin
    method: str  # annexure | sentence | none
    unsure: bool


def _segments(text: str) -> dict[str, list[str]]:
    """Annexure field -> candidate value texts, best label alternative first.

    A value runs from its label to the next field's label. Only each field's
    best alternative marks where values end; weaker alternatives still give
    fallback candidates (at most 400 characters), for forms whose canonical
    row is blank or garbled while a looser label holds the value.
    """
    found = {
        name: [match for pattern in patterns if (match := pattern.search(text))]
        for name, patterns in _LABEL_RES
    }
    starts = sorted(matches[0].start() for matches in found.values() if matches)
    values: dict[str, list[str]] = {}
    for name, matches in found.items():
        candidates = []
        for rank, match in enumerate(matches):
            following = [s for s in starts if s > match.start()]
            stop = following[0] if following else len(text)
            if rank > 0 or not following:
                stop = min(stop, match.end() + 400)
            candidates.append(_clean(text[match.end() : stop]))
        if candidates:
            values[name] = candidates
    return values


def _clean(value: str) -> str:
    value = _FRAGMENTS.sub(" ", value).replace("•", " ")  # bullet points
    value = re.sub(r"\s+", " ", value).strip(_STRIP)
    value = re.sub(r"(?:\s+(?:\d{1,2}|[a-z])[.)]?)+$", "", value)  # next row's number
    value = re.sub(r"^(?:\d{1,2}|[a-z])[.)]\s+", "", value)
    return value.strip(_STRIP)


def _counterparty(value: str) -> str | None:
    value = re.sub(r"^(?:m\s*/\s*s\.?|messrs\.?)\s*", "", value, flags=re.IGNORECASE).strip()
    if not value or value.lower() in {"-", "na", "n/a", "not applicable"}:
        return None
    return value[:200]


def _pick(amounts: list[Money]) -> Money | None:
    """Prefer a rupee amount (others cannot be compared with revenue)."""
    rupees = [m for m in amounts if m.currency == "INR" and m.amount >= MIN_ORDER_INR]
    chosen = rupees or [m for m in amounts if m.currency != "INR"]
    return chosen[0] if chosen else None


def _domestic_or_export(flat: str, fields: dict[str, str], amounts: list[Money]) -> str:
    answers = []
    for match in _DOM_LABEL.finditer(flat):
        window = _LABEL_WORD.sub(" ", flat[match.end() : match.end() + 60], count=1)
        answers.append(window)
    answers += [fields.get("dom_intl", ""), fields.get("awarded_by", "")]
    for answer in answers:
        head = answer.strip(_STRIP)[:30]
        if _EXPORT.search(head):
            return "export"
        if _DOMESTIC.search(head):
            return "domestic"
    # No usable answer field: only explicit export signals count (the label
    # text itself contains "international").
    if re.search(r"\bexport", flat, re.IGNORECASE) or any(
        m.currency not in (None, "INR") for m in amounts
    ):
        return "export"
    return "unknown"


_MONTHS = {
    name: number
    for number, names in enumerate(
        [
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ],
        start=1,
    )
    for name in names
}
_DAY_MONTH_YEAR = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})\b")
_MONTH_DAY_YEAR = re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b")


def _end_date(value: str) -> date | None:
    """The first date in ``value``: 31.03.2027, 31/03/2027, 31st March 2027,
    December 31, 2026."""
    found = _DATE.search(value)
    candidates: list[tuple[int, int, int]] = []
    if found:
        day, month, year = (int(g) for g in found.groups())
        candidates.append((year, month, day))
    for pattern, order in ((_DAY_MONTH_YEAR, "dmy"), (_MONTH_DAY_YEAR, "mdy")):
        match = pattern.search(value)
        if match:
            a, b, year_text = match.groups()
            name, day_text = (b, a) if order == "dmy" else (a, b)
            month_number = _MONTHS.get(name.lower())
            if month_number:
                candidates.append((int(year_text), month_number, int(day_text)))
    for year, month, day in candidates:
        try:
            return date(year, month, day)
        except ValueError:
            continue
    return None


def duration_months(value: str | None, filed: date | None) -> float | None:
    """Months from "18 months", "2 years", "70 days", or until an end date
    ("Up to 31.03.2027", "on or before December 31, 2026") counted from the
    filing date. None when nothing plausible (0-50 years) is found."""
    if not value:
        return None
    match = _DURATION.search(value)
    if match:
        n, unit = float(match.group(1)), match.group(2).lower()
        if unit.startswith(("year", "yr")):
            months = n * 12
        elif unit.startswith("day"):
            months = round(n / 30.4, 2)
        elif unit.startswith("week"):
            months = round(n / 4.35, 2)
        else:
            months = n
        return months if 0 < months <= 600 else None
    end = _end_date(value)
    if end is not None and filed is not None:
        months = (end - filed).days / 30.4
        return round(months, 1) if 0 < months <= 600 else None
    return None


def _sentence_amount(text: str) -> tuple[Money | None, str | None]:
    """The first amount in the most order-like sentence."""
    best: tuple[int, Money, str] | None = None
    for sentence in re.split(r"(?<=[.;])\s+(?=[A-Z])", text):
        if _NOT_ORDER_VALUE.search(sentence) or not _ORDER_WORDS.search(sentence):
            continue
        money = _pick(find_amounts(sentence))
        if money is None:
            continue
        score = 2 if _VALUE_WORDS.search(sentence) else 1
        if best is None or score > best[0]:
            best = (score, money, sentence.strip()[:1000])
    return (best[1], best[2]) if best else (None, None)


def extract_order_win(text: str, filed: date | None = None) -> RuleResult:
    """Order-win facts from attachment text. ``filed`` (the filing date)
    turns "to be executed by 31.03.2027" into months."""
    flat = " ".join(text.split())
    candidates = _segments(flat)
    fields = {name: values[0] for name, values in candidates.items()}
    repeat = bool(re.search(r"\brepeat order", flat, re.IGNORECASE)) or None
    domestic = _domestic_or_export(flat, fields, find_amounts(flat))
    counterparty = _counterparty(fields["counterparty"]) if "counterparty" in fields else None
    duration = duration_months(fields.get("period", ""), filed)

    money: Money | None = None
    evidence: str | None = None
    for size in candidates.get("size", []):
        money = _pick(find_amounts(size))
        if money is not None:
            evidence = size[:1000]
            break
    if money is not None:
        method, confidence = "annexure", 0.9
    else:
        money, evidence = _sentence_amount(flat)
        method, confidence = ("sentence", 0.7) if money else ("none", 0.2)
    order = OrderWin(
        amount_text=money.text if money else None,
        counterparty=counterparty,
        domestic_or_export=domestic,  # type: ignore[arg-type]
        duration_months=duration,
        is_repeat_order=repeat,
        confidence=confidence,
        evidence_span=evidence,
    )
    return RuleResult(order, method, unsure=confidence < 0.6)
