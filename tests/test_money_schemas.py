"""Amount normalisation and the OrderWin schema (T4.2).

Every phrasing below was found verbatim in real order-win attachments
(2026-09 filings, extracted in T4.1).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from gats.extract.money import find_amounts, parse_amount
from gats.extract.schemas import OrderWin

REAL_PHRASINGS: list[tuple[str, float, str | None]] = [
    ("Rs. 78.19 Crores", 781_900_000, "INR"),
    ("₹12 lakh", 1_200_000, "INR"),
    ("Rs 125.72 crore", 1_257_200_000, "INR"),
    ("Rs. 8,75,40,000/-", 87_540_000, "INR"),
    ("Rs.89.09 crores", 890_900_000, "INR"),
    ("160 crores", 1_600_000_000, "INR"),
    ("Rs. 130.58 Cr", 1_305_800_000, "INR"),
    ("92.41 Cr", 924_100_000, "INR"),
    ("0.400 Million", 400_000, None),
    ("₹ 76.06 crore", 760_600_000, "INR"),
    ("27,143 crores", 271_430_000_000, "INR"),
    ("INR 1265 crore", 12_650_000_000, "INR"),
    ("INR 2,000 Crore", 20_000_000_000, "INR"),
    ("Rs. 5,84,35,960/-", 58_435_960, "INR"),
    ("Rs. 24,35,974.30", 2_435_974.30, "INR"),
    ("Rs. 136 Lakhs", 13_600_000, "INR"),
    ("₹ 53.10 lakhs", 5_310_000, "INR"),
    ("Rs. 30.29 million", 30_290_000, "INR"),
    ("214.52 Cr.", 2_145_200_000, "INR"),
    ("INR 22.71 Crores", 227_100_000, "INR"),
    ("₹3,99,000/-", 399_000, "INR"),
    ("Rs. 23.40 Million", 23_400_000, "INR"),
    ("Rs. 23,397,835.32", 23_397_835.32, "INR"),
    ("₹4,48,15,000", 44_815_000, "INR"),
    ("1414.04 Lakhs", 141_404_000, "INR"),
    ("₹259.71 Cr", 2_597_100_000, "INR"),
    ("23 billion", 23_000_000_000, None),
    ("USD 10,150", 10_150, "USD"),
    ("US$9.78 mn", 9_780_000, "USD"),
    ("USD 1.2 billion", 1_200_000_000, "USD"),
    ("Rs. 903,00,68,492", 9_030_068_492, "INR"),
    ("INR 178.61 Lakhs", 17_861_000, "INR"),
    ("Rs. 97,58,393.94/-", 9_758_393.94, "INR"),
    ("₹ 14,98,65,900", 149_865_900, "INR"),
    ("INR 20,50,00,000", 205_000_000, "INR"),
    ("Rs. 63,15,14,433", 631_514_433, "INR"),
    ("19, CR", 190_000_000, "INR"),  # a table cell split by the PDF layout
    ("Rupees 450 Crore", 4_500_000_000, "INR"),
    ("Rs. 1, 303 Crores", 13_030_000_000, "INR"),  # KEC, 2026-09-14: a space after the comma
    ("in the range of Rs.10-17 crores", 100_000_000, "INR"),  # a range: its lower bound
]


@pytest.mark.parametrize(("text", "amount", "currency"), REAL_PHRASINGS)
def test_real_phrasings(text: str, amount: float, currency: str | None) -> None:
    money = parse_amount(text)
    assert money is not None
    assert money.amount == pytest.approx(amount)
    assert money.currency == currency
    assert money.amount_inr == (pytest.approx(amount) if currency == "INR" else None)


def test_at_least_thirty_real_phrasings() -> None:
    assert len(REAL_PHRASINGS) >= 30


@pytest.mark.parametrize(
    "text",
    ["rs ,", "orders 2 units", "ISIN INE12A01011 crore", "dated 03.10.2023 for 25 units", ""],
)
def test_non_amounts(text: str) -> None:
    assert parse_amount(text) is None


def test_a_list_is_not_one_split_number() -> None:
    found = find_amounts("orders of Rs. 100, 200 and 300 crore")
    assert [m.amount for m in found] == [100, 3_000_000_000]


def test_amounts_in_a_sentence() -> None:
    sentence = (
        "the Company, along with its wholly owned subsidiaries, have received purchase orders "
        "aggregating to Rs. 60 crore (USD 6.8 million) for the manufacturing of jewellery"
    )
    assert [(m.amount, m.currency) for m in find_amounts(sentence)] == [
        (600_000_000, "INR"),
        (6_800_000, "USD"),
    ]


class TestOrderWin:
    def test_numbers_come_from_the_verbatim_text(self) -> None:
        # The extractor claims 1.36 crore; the quoted text says 136 lakhs (= 1.36 crore
        # here, but the point is that the number is recomputed, not trusted).
        win = OrderWin(amount_text="Rs. 136 Lakhs", amount=999, currency="USD", confidence=0.9)
        assert (win.amount, win.currency, win.amount_inr) == (13_600_000, "INR", 13_600_000)

    def test_foreign_currency_has_no_rupee_value(self) -> None:
        win = OrderWin(amount_text="US$9.78 mn", domestic_or_export="export")
        assert (win.amount, win.currency, win.amount_inr) == (9_780_000, "USD", None)

    def test_no_amount_disclosed(self) -> None:
        win = OrderWin(amount=5.0, counterparty="Indian Railways")
        assert win.amount is None and win.amount_inr is None

    def test_invalid_values_are_rejected(self) -> None:
        with pytest.raises(ValidationError):
            OrderWin(amount_text="Rs. 60 crore", confidence=1.5)
        with pytest.raises(ValidationError):
            OrderWin(amount_text="sixty crore")  # no digits: not a parseable amount
        with pytest.raises(ValidationError):
            OrderWin(amount_text="Rs. 60 crore", surprise="field")  # type: ignore[call-arg]

    def test_json_schema_for_constrained_generation(self) -> None:
        schema = OrderWin.model_json_schema()
        assert "amount_text" in schema["properties"] and schema["additionalProperties"] is False


# Amounts in words, verbatim from real order-win texts (2026-09).
REAL_WORDS: list[tuple[str, float | None]] = [
    ("(Rupees One Crore Sixty-Four Lakh Ninety-Nine Thousand Seven Hundred Four only)", 16_499_704),
    (
        "(Rupees Eighteen Crore Forty -Two Lakh Six Thousand One Hundred Seventy -Six Only)",
        184_206_176,
    ),
    # a word split by the PDF layout
    ("(Rupees Two Crore Eighteen Lakhs Ninety-four Thousand Nine Hundre d Only)", 21_894_900),
    (
        "(Rupees One Hundred and Seventeen Crore Ninety Nine Lakh Seventy One Thousand Five "
        "Hundred and Sixty Four and Paise Ninety Only)",
        1_179_971_564.90,
    ),
    (
        "(Rupees Eighty-Three Lakh Five Thousand One Hundred Fifty-Three and Eighty-Seven "
        "paise only)",
        8_305_153.87,
    ),
    (
        "(Rupees Four Hundred And Four Crore Eighty-Eight Lakh Eighteen Thousand Seven Hundred "
        "And Thirty-Eight Rupees Only)",
        4_048_818_738,
    ),
    (
        "(Rupees Thirty Million Two Hundred Ninety Thousand and Two Hundred Seventy-One Only)",
        30_290_271,
    ),
    (
        "(Rupees Twenty Eight Crores Seventeen Lakhs Forty Two Thousand Three Hundred Eighty "
        "Two and Twenty Paise Only)",
        281_742_382.20,
    ),
    ("(Rupees Six Hundred & Sixty Crore and Seventy -Nine lakhs only)", 6_607_900_000),
    ("Rupees Fifty-Two Crore and fifty-five lakh only", 525_500_000),
    # a typo in the filing itself: unreadable, never guessed
    (
        "(Rupees Four Crore Fifteen Lakh Thifty-One Thousand Eight Hundred Seventeen and Paise "
        "Sixty Only)",
        None,
    ),
]


@pytest.mark.parametrize(("text", "amount"), REAL_WORDS)
def test_real_amounts_in_words(text: str, amount: float | None) -> None:
    money = parse_amount(text)
    if amount is None:
        assert money is None
    else:
        assert money is not None and money.currency == "INR"
        assert money.amount == pytest.approx(amount)


def test_words_rescue_a_figure_without_its_unit() -> None:
    # HEG Advanced Materials, 2026-09-18: "Crore" is missing after the figure (the
    # company later filed a corrigendum); the words carry it.
    found = find_amounts("Rs. 217.56 (Rupees Two Hundred Seventeen Crore Fifty Six Lakh only)")
    assert [m.amount for m in found] == [217.56, 2_175_600_000]


def test_scales_multiply_what_came_before() -> None:
    money = parse_amount("Rupees One Lakh Twenty Thousand Crore only")  # 1.2 lakh crore
    assert money is not None and money.amount == 1.2e12


@pytest.mark.parametrize("text", ["Rupees in Crores only", "Rs. Lakh only", "INR only"])
def test_words_that_are_not_amounts(text: str) -> None:
    assert parse_amount(text) is None
