"""Extraction accuracy against human labels (T4.6).

Three extractors are scored on the same labelled filings: the rules, the
LLM, and the cascade (rules, then the LLM when the rules are unsure).

What counts as right:

- **amount**: within 1% of the labelled amount in the same currency, or no
  amount when the label has none. A miss is split into *abstained* (gave no
  amount) and *wrong* (gave a different one): a wrong number does damage a
  missing one does not.
- **counterparty**: the same name after removing case, punctuation and
  company suffixes, or one name containing the other.
- **domestic_or_export**, **is_repeat_order**: equal.
- **duration_months**: within 10% or half a month.

Fields are scored only on filings the labeller marked as new orders; what
share of the sample those are is the taxonomy's precision for this type.

Anchoring: each labelled item showed one extractor's proposal, chosen at
random. If accepting proposals biased the labels, an extractor scores
better on the items where it was shown; the report puts the two side by
side.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from gats.extract.cascade import llm_answer
from gats.extract.rules import extract_order_win
from gats.extract.schemas import OrderWin
from gats.extract.texts import document_text
from gats.ingest import Services

METHODS = ("rules", "llm", "cascade")
FIELDS = ("amount", "counterparty", "domestic_or_export", "duration_months", "is_repeat_order")
_SUFFIXES = frozenset(
    {"ltd", "limited", "pvt", "private", "the", "m/s", "ms", "messrs", "co", "company"}
    | {"corporation", "corp", "inc", "llc", "of", "and"}
)
_COLUMNS = (
    "Extractor",
    "n",
    "Amount within 1% (95% CI)",
    "Abstained",
    "Wrong amount",
    "Counterparty",
    "Domestic/export",
    "Duration",
    "Repeat",
)


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """The 95% Wilson score interval for a proportion (well behaved near 0
    and 1 and for small n, unlike the normal approximation)."""
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    centre = p + z * z / (2 * n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((centre - half) / (1 + z * z / n), (centre + half) / (1 + z * z / n))


def _name_tokens(name: str | None) -> frozenset[str]:
    words = re.sub(r"[^a-z0-9/& ]+", " ", (name or "").lower()).split()
    return frozenset(w for w in words if w not in _SUFFIXES and w != "&")


def same_counterparty(predicted: str | None, labelled: str | None) -> bool:
    a, b = _name_tokens(predicted), _name_tokens(labelled)
    if not a or not b:
        return not a and not b
    return a <= b or b <= a


def _close(a: float | None, b: float | None, relative: float, absolute: float = 0.0) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= max(relative * abs(b), absolute)


def amount_outcome(predicted: OrderWin | None, label: Mapping[str, Any]) -> str:
    """correct | abstained | wrong (see the module docstring)."""
    want, want_currency = label.get("amount"), label.get("currency")
    got = predicted.amount if predicted else None
    got_currency = predicted.currency if predicted else None
    if want is None:
        return "correct" if got is None else "wrong"
    if got is None:
        return "abstained"
    return "correct" if got_currency == want_currency and _close(got, want, 0.01) else "wrong"


def field_results(predicted: OrderWin | None, label: Mapping[str, Any]) -> dict[str, bool]:
    blank = OrderWin()
    p = predicted or blank
    return {
        "amount": amount_outcome(predicted, label) == "correct",
        "counterparty": same_counterparty(p.counterparty, label.get("counterparty")),
        "domestic_or_export": p.domestic_or_export == label.get("domestic_or_export"),
        "duration_months": _close(p.duration_months, label.get("duration_months"), 0.10, 0.5),
        "is_repeat_order": p.is_repeat_order == label.get("is_repeat_order"),
    }


@dataclass
class Score:
    n: int = 0
    correct: dict[str, int] = field(default_factory=lambda: dict.fromkeys(FIELDS, 0))
    abstained: int = 0
    wrong_amount: int = 0

    def add(self, predicted: OrderWin | None, label: Mapping[str, Any]) -> None:
        self.n += 1
        for name, ok in field_results(predicted, label).items():
            self.correct[name] += ok
        outcome = amount_outcome(predicted, label)
        self.abstained += outcome == "abstained"
        self.wrong_amount += outcome == "wrong"

    def accuracy(self, name: str) -> float | None:
        return self.correct[name] / self.n if self.n else None


@dataclass
class Evaluation:
    labelled: int
    kinds: dict[str, int]
    scores: dict[str, Score]  # method -> score on new orders
    shown: dict[str, dict[bool, Score]]  # method -> {its proposal was shown: score}

    @property
    def new_orders(self) -> int:
        return self.kinds.get("new_order", 0)


def evaluate(
    labels: Sequence[Mapping[str, Any]],
    predictions: Mapping[str, Mapping[str, OrderWin | None]],
) -> Evaluation:
    """Score ``predictions[method][doc_id]`` against the labelled records."""
    done = [r for r in labels if r.get("status") == "labelled"]
    kinds: dict[str, int] = {}
    for record in done:
        kinds[str(record.get("kind"))] = kinds.get(str(record.get("kind")), 0) + 1
    scores = {method: Score() for method in predictions}
    shown: dict[str, dict[bool, Score]] = {
        method: {True: Score(), False: Score()} for method in predictions
    }
    for record in done:
        if record.get("kind") != "new_order" or not record.get("fields"):
            continue
        for method, by_doc in predictions.items():
            predicted = by_doc.get(record["doc_id"])
            scores[method].add(predicted, record["fields"])
            shown[method][record.get("shown") == method].add(predicted, record["fields"])
    return Evaluation(len(done), kinds, scores, shown)


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def render(result: Evaluation, *, target_labels: int, versions: Mapping[str, str]) -> str:
    """reports/M4_extraction.md."""
    lines = [
        "# M4 extraction accuracy (T4.6)",
        "",
        f"{result.labelled} human-labelled filings (target {target_labels}); "
        f"{result.new_orders} are new orders and are scored below.",
        "",
        "Labelled kinds: " + ", ".join(f"{k} {v}" for k, v in sorted(result.kinds.items())) + ".",
        "",
        "## Accuracy by extractor (new orders only)",
        "",
        "| " + " | ".join(_COLUMNS) + " |",
        "|" + "---|" * len(_COLUMNS),
    ]
    for method, score in result.scores.items():
        low, high = wilson(score.correct["amount"], score.n)
        share = [score.abstained / score.n, score.wrong_amount / score.n] if score.n else [None] * 2
        cells = [
            method,
            str(score.n),
            f"{_pct(score.accuracy('amount'))} ({low:.1%} to {high:.1%})",
            *(_pct(value) for value in share),
            *(_pct(score.accuracy(name)) for name in FIELDS[1:]),
        ]
        lines.append("| " + " | ".join(cells) + " |")
    lines += [
        "",
        "## Anchoring check (amount accuracy)",
        "",
        "Each item showed one extractor's proposal at random. A large gap between the two "
        "columns means labels leaned toward what was shown.",
        "",
        "| Extractor | Its proposal shown | Another's shown |",
        "|---|---|---|",
    ]
    for method, pair in result.shown.items():
        if method not in ("rules", "llm"):
            continue
        cells = [
            f"{_pct(pair[flag].accuracy('amount'))} (n={pair[flag].n})" for flag in (True, False)
        ]
        lines.append(f"| {method} | {cells[0]} | {cells[1]} |")
    cascade = result.scores.get("cascade")
    accuracy = cascade.accuracy("amount") if cascade else None
    enough = result.labelled >= target_labels
    if accuracy is None or not enough:
        verdict = f"not judged: {result.labelled} of {target_labels} labels"
    elif accuracy >= 0.90:
        verdict = "met"
    else:
        verdict = (
            "NOT met: a documented plan is due (better prompt, or the optional fine-tune T4.8)"
        )
    lines += [
        "",
        f"## Acceptance: cascade amount accuracy >= 90%: {verdict}",
        "",
        "Versions: " + "; ".join(f"{k} `{v}`" for k, v in versions.items()) + ".",
        "",
        "Limits: the sample is NSE filings of 2026-06 to 2026-08 only (held out from "
        "development); proposals were shown to the labeller (see the anchoring check).",
        "",
    ]
    return "\n".join(lines)


class LlmUnavailable(RuntimeError):
    """The LLM gave no answer for a labelled filing: scores would be wrong."""


async def predict(
    svc: Services, records: Sequence[Mapping[str, Any]], *, use_llm: bool = True
) -> dict[str, dict[str, OrderWin | None]]:
    """Each extractor's answer for every labelled document. LLM answers come
    from the cache when ``gats label prepare`` has run."""
    out: dict[str, dict[str, OrderWin | None]] = {"rules": {}}
    if use_llm:
        out |= {"llm": {}, "cascade": {}}
    for record in records:
        doc_id = record["doc_id"]
        filed = date.fromisoformat(record["filed"])
        with svc.engine.begin() as conn:
            text = document_text(conn, doc_id) or ""
        ruled = extract_order_win(text, filed=filed)
        out["rules"][doc_id] = ruled.order
        if not use_llm:
            continue
        answer, _ = await llm_answer(svc, doc_id, text, filed)
        if answer.status == "error":
            raise LlmUnavailable(f"no LLM answer for {doc_id[:10]}: {answer.error}")
        out["llm"][doc_id] = answer.order
        asked = ruled.unsure and answer.order is not None
        out["cascade"][doc_id] = answer.order if asked else ruled.order
    return out
