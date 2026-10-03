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
        None,
    ),
}


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
