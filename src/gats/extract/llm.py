"""Local LLM extraction through Ollama (T4.4).

The model fills a JSON schema in which every key is required and nothing is
numeric: it copies the amount, counterparty and duration as written and
picks domestic/export. Deterministic code then derives the numbers
(:class:`OrderWin`, :func:`duration_months`), and three guards keep it
honest:

1. the response must be JSON (``invalid_json`` otherwise),
2. it must match the schema exactly (``invalid_schema``),
3. the amount it quotes must be an amount that actually appears in the
   filing's text (``not_in_text``); an evidence quote that is not in the
   text is dropped.

Every call, valid or not, is stored in ``llm_extractions``, keyed by
(document, prompt hash, model): the cache that avoids paying twice, and the
log that shows how often the model fails. The prompt is versioned by the
hash of everything that shapes the output (system text, schema, limits).
The cache holds the model's *reply*; validation runs again on every use, so
a fix to the guards applies without calling the model again.

Verified 2026-10-03 against Ollama 0.32.8: ``POST /api/chat`` with
``format`` set to a JSON schema returns schema-shaped JSON in
``message.content``; temperature 0 and a fixed seed make it repeatable.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from gats.config import Settings
from gats.extract.money import find_amounts, parse_amount
from gats.extract.rules import duration_months
from gats.extract.schemas import MIN_ORDER_INR, OrderWin
from gats.net import FetchError, PoliteClient

LLM_CONFIDENCE = 0.75

SYSTEM_ORDER_WIN = (
    "You read Indian stock-exchange filings in which a listed company discloses an order or "
    "contract it has won, and you fill in the JSON fields.\n"
    "- amount_text: the order or contract value exactly as written, with its currency and "
    "unit (e.g. 'Rs. 60 crore', 'USD 12.5 million'); null if no value is stated. Never the "
    "company's turnover, net worth, share capital, EMD or bank guarantee.\n"
    "- counterparty: who awarded or placed the order, as written; null if not stated.\n"
    "- domestic_or_export: 'export' if the customer is outside India, 'domestic' if in India, "
    "otherwise 'unknown'.\n"
    "- duration_text: the execution period or completion date as written; null if not stated.\n"
    "- is_repeat_order: true only if the text calls it a repeat order; null if it does not say.\n"
    "- evidence_span: one sentence copied from the text that states the order and its value.\n"
    "Copy text verbatim. Do not compute, convert or guess."
)


class OrderWinLLM(BaseModel):
    """What the model must return: every key, no numbers."""

    model_config = ConfigDict(extra="forbid")

    amount_text: str | None
    counterparty: str | None
    domestic_or_export: Literal["domestic", "export", "unknown"]
    duration_text: str | None
    is_repeat_order: bool | None
    evidence_span: str | None


def prompt_hash(max_chars: int) -> str:
    """Version of everything that shapes the model's output."""
    spec = {
        "system": SYSTEM_ORDER_WIN,
        "schema": OrderWinLLM.model_json_schema(),
        "max_chars": max_chars,
        "options": {"temperature": 0, "seed": 0},
    }
    return hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:12]


@dataclass
class LlmResult:
    # ok | invalid_json | invalid_schema | not_in_text | unparseable_amount |
    # implausible_amount | error
    status: str
    order: OrderWin | None = None
    raw: str | None = None
    error: str | None = None
    latency_ms: int | None = None
    prompt_tokens: int | None = None
    output_tokens: int | None = None


def _norm(text: str) -> str:
    text = re.sub(r"\b(?:m\s*/\s*s|mis|messrs)\.?\s+", "", text.lower())
    return re.sub(r"[\s,]+", " ", text).strip()


def amount_supported(amount_text: str, text: str) -> bool:
    """The quoted amount is in the text: verbatim, or as the same value."""
    if _norm(amount_text) in _norm(text):
        return True
    claimed = parse_amount(amount_text)
    if claimed is None:
        return False
    return any(
        m.currency == claimed.currency
        and abs(m.amount - claimed.amount) <= 0.005 * max(claimed.amount, 1.0)
        for m in find_amounts(text)
    )


def validate_response(content: str, text: str, filed: date | None) -> LlmResult:
    """Turn the model's message into an :class:`OrderWin`, or say why not."""
    try:
        data: Any = json.loads(content)
    except json.JSONDecodeError as exc:
        return LlmResult("invalid_json", raw=content, error=str(exc)[:500])
    try:
        parsed = OrderWinLLM.model_validate(data)
    except ValidationError as exc:
        return LlmResult("invalid_schema", raw=content, error=str(exc)[:500])
    if parsed.amount_text and not amount_supported(parsed.amount_text, text):
        return LlmResult(
            "not_in_text", raw=content, error=f"amount {parsed.amount_text!r} not in the text"
        )
    evidence = parsed.evidence_span
    if evidence and _norm(evidence) not in _norm(text):
        evidence = None  # paraphrased quotes are not evidence
    try:
        order = OrderWin(
            amount_text=parsed.amount_text,
            counterparty=parsed.counterparty,
            domestic_or_export=parsed.domestic_or_export,
            duration_months=duration_months(parsed.duration_text, filed),
            is_repeat_order=parsed.is_repeat_order,
            confidence=LLM_CONFIDENCE,
            evidence_span=evidence[:1000] if evidence else None,
        )
    except ValidationError as exc:
        return LlmResult("unparseable_amount", raw=content, error=str(exc)[:500])
    if order.currency == "INR" and order.amount is not None and order.amount < MIN_ORDER_INR:
        return LlmResult(
            "implausible_amount", raw=content, error=f"{order.amount_text!r} is below 1 lakh"
        )
    return LlmResult("ok", order=order, raw=content)


def model_input(text: str, settings: Settings) -> str:
    """The part of the filing the model sees (and its answer is checked against)."""
    return text[: settings.llm_max_chars]


async def llm_order_win(
    client: PoliteClient, settings: Settings, text: str, filed: date | None
) -> LlmResult:
    """One extraction call (no caching here; see gats.extract.cascade)."""
    payload = {
        "model": settings.llm_model,
        "messages": [
            {"role": "system", "content": SYSTEM_ORDER_WIN},
            {"role": "user", "content": model_input(text, settings)},
        ],
        "format": OrderWinLLM.model_json_schema(),
        "options": {"temperature": 0, "seed": 0},
        "stream": False,
    }
    url = settings.ollama_url.rstrip("/") + "/api/chat"
    try:
        got = await client.post_json(url, payload, timeout_s=settings.llm_timeout_s)
    except FetchError as exc:
        return LlmResult("error", error=str(exc)[:500])
    if not got.ok:
        return LlmResult(
            "error", error=f"HTTP {got.status}: {got.content[:200]!r}", latency_ms=got.elapsed_ms
        )
    try:
        body = json.loads(got.content)
        content = str(body["message"]["content"])
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        return LlmResult("error", error=f"unexpected Ollama response: {exc}"[:500])
    result = validate_response(content, model_input(text, settings), filed)
    result.latency_ms = got.elapsed_ms
    result.prompt_tokens = body.get("prompt_eval_count")
    result.output_tokens = body.get("eval_count")
    return result
