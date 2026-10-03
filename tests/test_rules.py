"""ORDER_WIN rules baseline (T4.3), on real attachment texts."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from gats.extract.rules import extract_order_win

TEXTS: dict[str, str] = json.loads(
    (Path(__file__).parent / "fixtures" / "real" / "order_win_texts.json").read_text(
        encoding="utf-8"
    )
)
FILED = date(2026, 9, 15)

# Verified by hand against each text.
EXPECTED = {
    # annexure with interleaved cells ("Whether domestic or Domestic internationa l")
    "HEC Infra Projects Limited": ("annexure", 74_500_000, "Nakshatra Infra", "domestic", 6.0),
    # "Time period ... Up to 31.03.2027": months from the filing date
    "Ceinsys Tech Limited": ("annexure", 102_728_092, "MSRDC Tunnels Limited", "domestic", 6.5),
    "NBCC (India) Limited": ("annexure", 19_200_000, "KVS, NEW DELHI", "domestic", None),
    # bullets, an Ugandan buyer, "70 days"
    "Veerhealth Care Ltd": ("annexure", 13_600_000, "Vision lmpex Limited", "export", 2.3),
    "Goldiam International Limited": (
        "annexure",
        600_000_000,
        "International USA clients",
        "export",
        3.5,  # "on or before December 31, 2026", from the filing date
    ),
}


# Found by comparing the rules with the LLM in T4.4; verified by hand.
REGRESSIONS = {
    # "a Supply of Goods Contract valued at approximately Rs 58.92 crores" is one of two
    # contracts; "Broad consideration or size" holds the total
    "Laser Power & Infra Limited": 727_700_000,
    # the "Broad consideration" row holds tender text and "Value of the order(s)" the
    # amount; a signature stamp "RS20" is no order value
    "BCPL Railway Infrastructure Limited": 58_900_000,
    # "Rs. 217.56" without its unit; the amount in words says crore
    "HEG Advanced Materials Limited": 2_175_600_000,
    # the PDF splits the figure ("Rs. 6 60.79/-"); the words are intact
    "Vascon Engineers Ltd": 6_607_900_000,
}


@pytest.mark.parametrize("company", sorted(REGRESSIONS))
def test_real_regressions(company: str) -> None:
    result = extract_order_win(TEXTS[company], filed=FILED)
    assert result.method == "annexure" and not result.unsure
    assert result.order.amount_inr == pytest.approx(REGRESSIONS[company])


def test_canonical_label_beats_prose() -> None:
    result = extract_order_win(TEXTS["Laser Power & Infra Limited"], filed=FILED)
    assert result.order.counterparty == "Power Grid Corporation of India Limited"
    assert result.order.evidence_span and "72.77" in result.order.evidence_span


def test_stamp_sized_amounts_are_not_order_values() -> None:
    result = extract_order_win(
        "The Company has received an order from XYZ Ltd, signed RS20 Company Secretary"
    )
    assert result.method == "none" and result.order.amount is None


@pytest.mark.parametrize("company", sorted(EXPECTED))
def test_real_annexures(company: str) -> None:
    method, amount, counterparty, where, months = EXPECTED[company]
    result = extract_order_win(TEXTS[company], filed=FILED)
    assert result.method == method and not result.unsure
    assert result.order.amount_inr == pytest.approx(amount)
    assert result.order.counterparty == counterparty
    assert result.order.domestic_or_export == where
    assert result.order.duration_months == (pytest.approx(months) if months else None)
    assert result.order.evidence_span


def test_covering_letter_sentence() -> None:
    text = (
        "Dear Sir, We are pleased to inform that the Company has received a repeat order worth "
        "Rs. 42.5 crore from Bharat Heavy Electricals Limited for supply of transformers. "
        "The company's turnover last year was Rs. 900 crore."
    )
    result = extract_order_win(text)
    assert result.method == "sentence" and result.order.confidence == 0.7
    assert result.order.amount_inr == pytest.approx(425_000_000)  # not the turnover
    assert result.order.is_repeat_order is True


def test_foreign_order_without_annexure_is_export() -> None:
    result = extract_order_win("The Company has secured a contract valued at USD 12.5 million.")
    assert result.order.currency == "USD" and result.order.amount_inr is None
    assert result.order.domestic_or_export == "export"


def test_nothing_found_is_unsure() -> None:
    result = extract_order_win(
        "Please find enclosed the intimation. Kindly take the same on record."
    )
    assert result.method == "none" and result.unsure
    assert result.order.amount is None and result.order.confidence == 0.2


@pytest.mark.parametrize(
    "junk",
    ["", "   ", "Rs ,", "\x00\x01", "Time period 9999 years", "Broad consideration or size -"],
)
def test_never_raises(junk: str) -> None:
    extract_order_win(junk, filed=FILED)
