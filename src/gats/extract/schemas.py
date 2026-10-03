"""Output schemas for extracted facts (T4.2).

Division of labour: the extractor (rules or LLM) reports what the filing
*says*: the verbatim amount text, the counterparty, a quote as evidence.
Deterministic code then derives the numbers: :class:`OrderWin` recomputes
``amount``, ``currency`` and ``amount_inr`` from ``amount_text``, so a model
that misreads "136 lakh" as 1.36 crore cannot slip a wrong number past the
validator. The schema is also what the LLM is constrained to produce
(``model_json_schema()``).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from gats.extract.money import parse_amount

Currency = Literal["INR", "USD", "EUR", "GBP", "AED"]


class OrderWin(BaseModel):
    """One order or contract win, as disclosed."""

    model_config = ConfigDict(extra="forbid")

    amount_text: str | None = Field(
        default=None,
        max_length=120,
        description="The amount exactly as written, e.g. 'Rs. 60 crore'.",
    )
    amount: float | None = Field(default=None, ge=0, description="Derived from amount_text.")
    currency: Currency | None = Field(default=None, description="Derived from amount_text.")
    amount_inr: float | None = Field(
        default=None, ge=0, description="Rupees only; never FX-converted."
    )
    counterparty: str | None = Field(
        default=None, max_length=200, description="Who placed the order."
    )
    domestic_or_export: Literal["domestic", "export", "unknown"] = "unknown"
    duration_months: float | None = Field(default=None, gt=0, le=240)
    is_repeat_order: bool | None = None
    confidence: float = Field(default=0.5, ge=0, le=1)
    evidence_span: str | None = Field(
        default=None, max_length=1000, description="A verbatim quote supporting the amount."
    )

    @model_validator(mode="after")
    def _derive_amount(self) -> OrderWin:
        if self.amount_text is None:
            self.amount = self.currency = self.amount_inr = None
            return self
        money = parse_amount(self.amount_text)
        if money is None:
            raise ValueError(f"amount_text {self.amount_text!r} is not an amount")
        self.amount = money.amount
        self.currency = money.currency  # type: ignore[assignment]
        self.amount_inr = money.amount_inr
        return self


SCHEMAS: dict[str, type[BaseModel]] = {"ORDER_WIN": OrderWin}
