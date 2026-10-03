"""Extraction accuracy against labels (T4.6)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select

from gats.db.schema import announcements
from gats.extract.evaluate import (
    LlmUnavailable,
    amount_outcome,
    evaluate,
    field_results,
    predict,
    render,
    same_counterparty,
    wilson,
)
from gats.extract.schemas import OrderWin
from gats.ingest import Services
from tests.conftest import seed_filings
from tests.test_llm_cascade import SURE, UNSURE, answer, chat, chat_url


def test_wilson_interval_matches_the_standard_value() -> None:
    low, high = wilson(8, 10)
    assert (low, high) == (pytest.approx(0.4902, abs=1e-4), pytest.approx(0.9433, abs=1e-4))
    assert wilson(0, 0) == (0.0, 1.0)
    assert wilson(10, 10)[1] == pytest.approx(1.0) and wilson(10, 10)[0] < 1.0


@pytest.mark.parametrize(
    ("predicted", "labelled", "same"),
    [
        ("M/s NTPC Limited", "NTPC Ltd", True),
        ("Bharat Heavy Electricals Ltd (BHEL)", "Bharat Heavy Electricals Limited", True),
        ("Power Grid Corporation of India Limited", "Power Grid Corporation Of India Ltd.", True),
        ("NTPC Limited", "Power Grid Corporation of India", False),
        (None, None, True),
        (None, "NTPC Limited", False),
        ("a leading PSU", None, False),
    ],
)
def test_counterparty_matching(predicted: str | None, labelled: str | None, same: bool) -> None:
    assert same_counterparty(predicted, labelled) is same


def label(
    amount: float | None = 425_000_000, currency: str | None = "INR", **fields: Any
) -> dict[str, Any]:
    return {
        "amount": amount,
        "currency": currency,
        "counterparty": "NTPC Limited",
        "domestic_or_export": "domestic",
        "duration_months": 18.0,
        "is_repeat_order": None,
    } | fields


def test_amount_outcomes() -> None:
    right = OrderWin(amount_text="Rs. 42.3 crore")  # within 1% of 42.5
    assert amount_outcome(right, label()) == "correct"
    assert amount_outcome(OrderWin(amount_text="Rs. 40 crore"), label()) == "wrong"
    assert amount_outcome(OrderWin(), label()) == amount_outcome(None, label()) == "abstained"
    assert amount_outcome(OrderWin(), label(None, None)) == "correct"  # none stated, none claimed
    assert amount_outcome(right, label(None, None)) == "wrong"  # claimed one that is not there
    dollars = OrderWin(amount_text="USD 12.5 million")
    assert amount_outcome(dollars, label(12_500_000, "USD")) == "correct"
    assert amount_outcome(dollars, label(12_500_000, "INR")) == "wrong"  # right number, wrong money


def test_field_results() -> None:
    order = OrderWin(
        amount_text="Rs. 42.5 crore",
        counterparty="NTPC Ltd",
        domestic_or_export="domestic",
        duration_months=17.0,
    )
    assert field_results(order, label()) == dict.fromkeys(
        ["amount", "counterparty", "domestic_or_export", "duration_months", "is_repeat_order"], True
    )
    assert field_results(order, label(duration_months=24.0))["duration_months"] is False
    assert (
        field_results(None, label())["domestic_or_export"] is False
    )  # "unknown" is not "domestic"


def record(
    doc: str, kind: str = "new_order", shown: str = "rules", **fields: Any
) -> dict[str, Any]:
    return {
        "doc_id": doc,
        "status": "labelled",
        "kind": kind,
        "shown": shown,
        "filed": "2026-07-14",
        "fields": label(**fields) if kind == "new_order" else None,
    }


def test_scores_kinds_and_the_anchoring_split() -> None:
    labels = [
        record("a", shown="rules"),
        record("b", shown="llm"),
        record("c", shown="llm", amount=None, currency=None),
        record("d", kind="lowest_bidder"),
        {"doc_id": "e", "status": "skipped", "kind": None, "shown": "rules", "fields": None},
    ]
    good = OrderWin(
        amount_text="Rs. 42.5 crore",
        counterparty="NTPC Limited",
        domestic_or_export="domestic",
        duration_months=18,
    )
    predictions = {
        "rules": {"a": good, "b": OrderWin(), "c": OrderWin()},
        "llm": {"a": good, "b": good, "c": OrderWin(amount_text="Rs. 9 crore")},
    }
    result = evaluate(labels, predictions)
    assert result.labelled == 4 and result.kinds == {"new_order": 3, "lowest_bidder": 1}
    rules, llm = result.scores["rules"], result.scores["llm"]
    assert (rules.n, rules.correct["amount"], rules.abstained, rules.wrong_amount) == (3, 2, 1, 0)
    assert (llm.n, llm.correct["amount"], llm.abstained, llm.wrong_amount) == (3, 2, 0, 1)
    assert result.shown["rules"][True].n == 1 and result.shown["rules"][False].n == 2
    assert result.shown["llm"][True].accuracy("amount") == 0.5

    text = render(result, target_labels=300, versions={"rules": "v"})
    assert "not judged: 4 of 300 labels" in text and "| rules | 3 | 66.7%" in text
    assert "| llm | 50.0% (n=2) | 100.0% (n=1) |" in text


@pytest.mark.parametrize(("accuracy", "verdict"), [(9, ": met"), (8, ": NOT met")])
def test_acceptance_is_stated(accuracy: int, verdict: str) -> None:
    labels = [record(str(i)) for i in range(10)]
    good = OrderWin(amount_text="Rs. 42.5 crore")
    cascade = {str(i): (good if i < accuracy else OrderWin()) for i in range(10)}
    text = render(evaluate(labels, {"cascade": cascade}), target_labels=10, versions={})
    assert f"cascade amount accuracy >= 90%{verdict}" in text


@respx.mock
async def test_predictions_for_all_three_extractors(svc: Services) -> None:
    seed_filings(svc.engine, svc.store, [{"text": SURE}, {"text": UNSURE}])
    with svc.engine.begin() as conn:
        sure_doc, unsure_doc = conn.execute(
            select(announcements.c.attachment_doc_id).order_by(announcements.c.id)
        ).scalars()

    def model(request: httpx.Request) -> httpx.Response:
        text = json.loads(request.content)["messages"][1]["content"]
        amount = "Rs. 10 crore" if "Rs. 10 crore" in text else "Rs. 42.5 crore"
        return httpx.Response(200, json=chat(answer(amount_text=amount, evidence_span=None)))

    respx.post(chat_url(svc)).mock(side_effect=model)
    records = [record(sure_doc), record(unsure_doc)]
    out = await predict(svc, records)
    assert set(out) == {"rules", "llm", "cascade"}
    assert out["rules"][unsure_doc].amount is None  # type: ignore[union-attr]
    assert out["llm"][unsure_doc].amount_inr == 425_000_000  # type: ignore[union-attr]
    assert out["cascade"][unsure_doc].amount_inr == 425_000_000  # type: ignore[union-attr]
    assert out["cascade"][sure_doc] == out["rules"][sure_doc]  # the rules were sure: no LLM
    assert set(await predict(svc, records, use_llm=False)) == {"rules"}


@respx.mock
async def test_a_silent_llm_stops_the_evaluation(svc: Services) -> None:
    seed_filings(svc.engine, svc.store, [{"text": UNSURE}])
    with svc.engine.begin() as conn:
        doc = conn.execute(select(announcements.c.attachment_doc_id)).scalar_one()
    respx.post(chat_url(svc)).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(LlmUnavailable):
        await predict(svc, [record(doc)])
