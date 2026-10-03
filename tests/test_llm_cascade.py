"""Local LLM extraction and the rules -> LLM cascade (T4.4).

Ollama is mocked with respx: no network, no model. Replies have the shape of
real Ollama /api/chat responses (verified 2026-10-03, see DATA_SOURCES.md).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select

from gats.db import repo
from gats.db.schema import (
    announcement_event_types,
    announcements,
    document_texts,
    extractions,
    llm_extractions,
)
from gats.extract.cascade import code_hash, run_extractions
from gats.extract.llm import (
    LLM_CONFIDENCE,
    OrderWinLLM,
    amount_supported,
    llm_order_win,
    prompt_hash,
    validate_response,
)
from gats.extract.pdf_text import EXTRACTOR, EXTRACTOR_VERSION
from gats.extract.rules import RULES_VERSION
from gats.ingest import Services
from gats.sources.models import AnnouncementRecord

NOW = datetime(2026, 10, 3, tzinfo=UTC)
FILED = date(2026, 9, 15)
TAXONOMY = "taxonomy-test"

TEXT = (
    "Dear Sir, the Company has received a work order from Bharat Heavy Electricals Limited "
    "for supply of transformers, to be completed within 18 months. The order is worth "
    "Rs. 42.5 crore."
)
# The rules find this annexure and are sure of it.
SURE = (
    "1. Name of the entity awarding the order(s)/contract(s) NTPC Limited 7. Broad "
    "consideration or size of the order(s)/contract(s) Rs. 10 crore 8. Whether the promoter "
    "group has any interest No"
)
# No order word near the amount: the rules find nothing and are unsure.
UNSURE = "The Company has bagged a mandate from NTPC Limited. Value: Rs. 42.5 crore."


def answer(**fields: Any) -> str:
    """The model's message: JSON with every key the schema requires."""
    values: dict[str, Any] = {
        "amount_text": "Rs. 42.5 crore",
        "counterparty": "Bharat Heavy Electricals Limited",
        "domestic_or_export": "domestic",
        "duration_text": "within 18 months",
        "is_repeat_order": None,
        "evidence_span": "The order is worth Rs. 42.5 crore.",
    }
    return json.dumps(values | fields)


def chat(content: str | None = None) -> dict[str, Any]:
    """An Ollama /api/chat response body."""
    return {
        "model": "qwen2.5-coder:7b-instruct-q4_K_M",
        "message": {"role": "assistant", "content": answer() if content is None else content},
        "done": True,
        "prompt_eval_count": 812,
        "eval_count": 64,
    }


def chat_url(svc: Services) -> str:
    return svc.settings.ollama_url.rstrip("/") + "/api/chat"


class TestValidation:
    def test_ok_numbers_come_from_code(self) -> None:
        result = validate_response(answer(), TEXT, FILED)
        assert result.status == "ok" and result.order is not None
        assert result.order.amount_inr == pytest.approx(425_000_000)
        assert result.order.duration_months == 18
        assert result.order.confidence == LLM_CONFIDENCE
        assert result.order.evidence_span == "The order is worth Rs. 42.5 crore."

    @pytest.mark.parametrize(
        ("content", "status"),
        [
            ("Sure! The amount is Rs. 42.5 crore.", "invalid_json"),
            (json.dumps({"amount_text": "Rs. 42.5 crore"}), "invalid_schema"),  # keys missing
            (answer(amount=425_000_000), "invalid_schema"),  # numbers are not the model's job
            (answer(domestic_or_export="India"), "invalid_schema"),
            (answer(amount_text="Rs. 99 crore"), "not_in_text"),  # invented
        ],
    )
    def test_failures_are_named(self, content: str, status: str) -> None:
        result = validate_response(content, TEXT, FILED)
        assert result.status == status and result.order is None
        assert result.raw == content and result.error

    def test_a_quote_that_is_no_amount(self) -> None:
        text = "We have an order of about forty-two crore."
        result = validate_response(answer(amount_text="about forty-two crore"), text, FILED)
        assert result.status == "unparseable_amount"

    def test_a_stamp_is_no_order_value(self) -> None:
        text = TEXT + " For Example Ltd, Signed RS20 Company Secretary"
        result = validate_response(answer(amount_text="RS20"), text, FILED)
        assert result.status == "implausible_amount"

    def test_paraphrased_evidence_is_dropped(self) -> None:
        content = answer(evidence_span="BHEL ordered transformers worth 42.5 cr")
        result = validate_response(content, TEXT, FILED)
        assert result.status == "ok" and result.order is not None
        assert result.order.evidence_span is None

    def test_amount_supported_by_value_as_well_as_verbatim(self) -> None:
        assert amount_supported("Rs. 42.5 crore", TEXT)
        assert amount_supported("INR 42.50 Crores", TEXT)
        assert not amount_supported("INR 4.25 Crores", TEXT)


def test_prompt_hash_tracks_what_shapes_the_output() -> None:
    assert prompt_hash(8000) == prompt_hash(8000)
    assert prompt_hash(8000) != prompt_hash(4000)
    assert len(prompt_hash(8000)) == 12


class TestCall:
    @respx.mock
    async def test_request_pins_schema_and_sampling(self, svc: Services) -> None:
        route = respx.post(chat_url(svc)).mock(return_value=httpx.Response(200, json=chat()))
        result = await llm_order_win(svc.client, svc.settings, TEXT, FILED)
        assert result.status == "ok"
        assert (result.prompt_tokens, result.output_tokens) == (812, 64)
        sent = json.loads(route.calls.last.request.content)
        assert sent["model"] == svc.settings.llm_model
        assert sent["format"] == OrderWinLLM.model_json_schema()
        assert sent["options"] == {"temperature": 0, "seed": 0} and sent["stream"] is False
        assert sent["messages"][1]["content"] == TEXT

    @respx.mock
    async def test_the_answer_is_checked_against_what_the_model_saw(self, svc: Services) -> None:
        respx.post(chat_url(svc)).mock(return_value=httpx.Response(200, json=chat()))
        settings = svc.settings.model_copy(update={"llm_max_chars": 60})
        result = await llm_order_win(svc.client, settings, TEXT, FILED)
        assert result.status == "not_in_text"  # the amount is past the cut

    @pytest.mark.parametrize(
        "response",
        [
            httpx.Response(500, text="model crashed"),
            httpx.Response(404, json={"error": "model 'x' not found"}),
            httpx.Response(200, json={"unexpected": True}),
            httpx.ConnectError("connection refused"),
        ],
    )
    @respx.mock
    async def test_no_answer_is_an_error(
        self, svc: Services, response: httpx.Response | Exception
    ) -> None:
        route = respx.post(chat_url(svc))
        if isinstance(response, Exception):
            route.mock(side_effect=response)
        else:
            route.mock(return_value=response)
        result = await llm_order_win(svc.client, svc.settings, TEXT, FILED)
        assert result.status == "error" and result.order is None and result.raw is None


# --- the cascade ------------------------------------------------------------------


def seed(svc: Services, texts: list[str]) -> list[int]:
    """One ORDER_WIN filing per text, its attachment downloaded and read."""
    records = [
        AnnouncementRecord(
            source="NSE",
            source_ann_id=str(i),
            symbol=f"CO{i}",
            scrip_code=None,
            isin=None,
            company_name=f"Company {i}",
            category="Bagging/Receiving of orders/contracts",
            subcategory=None,
            subject=None,
            details=None,
            attachment_url=f"https://example.com/{i}.pdf",
            exch_submitted_ts=None,
            exch_disseminated_ts=NOW,
            event_ts=NOW,
        )
        for i in range(len(texts))
    ]
    ids = []
    with svc.engine.begin() as conn:
        page = repo.save_raw(
            conn,
            svc.store,
            b"page",
            kind="t",
            source="NSE",
            url="u",
            content_type=None,
            fetched_at=NOW,
        )
        repo.insert_announcements(
            conn,
            records,
            raw_doc_id=page,
            parser_version="v",
            mode="backfill",
            fetched_at=NOW,
            now=NOW,
        )
        for i, text in enumerate(texts):
            ann_id = int(
                conn.execute(
                    select(announcements.c.id).where(announcements.c.source_ann_id == str(i))
                ).scalar_one()
            )
            doc = repo.save_raw(
                conn,
                svc.store,
                f"pdf {i}".encode(),
                kind="attachment",
                source="NSE",
                url=f"https://example.com/{i}.pdf",
                content_type="application/pdf",
                fetched_at=NOW,
            )
            repo.mark_attachment(conn, ann_id, status="done", doc_id=doc)
            conn.execute(
                announcement_event_types.insert().values(
                    announcement_id=ann_id,
                    taxonomy_version=TAXONOMY,
                    event_type="ORDER_WIN",
                    rule_no=0,
                    classified_at=NOW,
                )
            )
            conn.execute(
                document_texts.insert().values(
                    doc_id=doc,
                    extractor=EXTRACTOR,
                    extractor_version=EXTRACTOR_VERSION,
                    pages=1,
                    chars=len(text),
                    needs_ocr=False,
                    error=None,
                    text=text,
                    extracted_at=NOW,
                )
            )
            ids.append(ann_id)
    return ids


def by_filing(svc: Services, version_prefix: str) -> dict[int, Any]:
    with svc.engine.begin() as conn:
        rows = conn.execute(
            select(extractions).where(extractions.c.extractor_version.startswith(version_prefix))
        ).all()
    return {row.announcement_id: row for row in rows}


def quoting_the_input(request: httpx.Request) -> httpx.Response:
    """A model that answers with whichever amount its input contains."""
    text = json.loads(request.content)["messages"][1]["content"]
    amount = "Rs. 10 crore" if "Rs. 10 crore" in text else "Rs. 42.5 crore"
    return httpx.Response(200, json=chat(answer(amount_text=amount, evidence_span=None)))


async def run(svc: Services, mode: str, limit: int = 100) -> Any:
    return await run_extractions(
        svc,
        event_type="ORDER_WIN",
        taxonomy_version=TAXONOMY,
        mode=mode,  # type: ignore[arg-type]
        limit=limit,
    )


@respx.mock
async def test_rules_mode_never_calls_the_model(svc: Services) -> None:
    route = respx.post(chat_url(svc)).mock(side_effect=quoting_the_input)
    sure, unsure = seed(svc, [SURE, UNSURE])
    stats = await run(svc, "rules")
    assert stats.version == f"rules:{RULES_VERSION}+{code_hash()}"
    assert dict(stats.by_method) == {"rules_annexure": 1, "rules_none": 1}
    assert route.call_count == 0
    rows = by_filing(svc, "rules:")
    assert rows[sure].amount_inr == pytest.approx(100_000_000) and rows[sure].confidence == 0.9
    assert rows[unsure].amount_inr is None
    assert (await run(svc, "rules")).filings == 0  # re-runnable: nothing left to do


@respx.mock
async def test_cascade_asks_the_model_only_when_the_rules_are_unsure(svc: Services) -> None:
    route = respx.post(chat_url(svc)).mock(side_effect=quoting_the_input)
    sure, unsure = seed(svc, [SURE, UNSURE])
    stats = await run(svc, "cascade")
    assert dict(stats.by_method) == {"rules_annexure": 1, "llm": 1}
    assert route.call_count == 1 and stats.llm_calls == 1
    rows = by_filing(svc, "cascade:")
    assert rows[unsure].method == "llm" and rows[unsure].amount_inr == pytest.approx(425_000_000)
    assert rows[unsure].confidence == LLM_CONFIDENCE
    assert rows[sure].method == "rules_annexure"
    with svc.engine.begin() as conn:
        logged = conn.execute(select(llm_extractions)).one()
    assert logged.status == "ok" and logged.prompt_tokens == 812 and logged.raw

    # LLM-only mode reuses that reply and pays only for the other filing.
    stats = await run(svc, "llm")
    assert (stats.llm_calls, stats.llm_cached, route.call_count) == (1, 1, 2)
    rows = by_filing(svc, "llm:")
    assert rows[sure].amount_inr == pytest.approx(100_000_000)


@respx.mock
async def test_cached_replies_are_validated_again(svc: Services) -> None:
    """A reply stored as ok by older, laxer code fails today's guards."""
    route = respx.post(chat_url(svc)).mock(side_effect=quoting_the_input)
    (unsure,) = seed(svc, [UNSURE])
    with svc.engine.begin() as conn:
        doc = conn.execute(select(announcements.c.attachment_doc_id)).scalar_one()
        conn.execute(
            llm_extractions.insert().values(
                doc_id=doc,
                event_type="ORDER_WIN",
                prompt_hash=prompt_hash(svc.settings.llm_max_chars),
                model=svc.settings.llm_model,
                status="ok",
                output=None,
                raw=answer(amount_text="Rs. 99 crore"),
                created_at=NOW,
            )
        )
    stats = await run(svc, "cascade")
    assert route.call_count == 0 and stats.llm_cached == 1
    assert dict(stats.llm_status) == {"not_in_text": 1}
    assert by_filing(svc, "cascade:")[unsure].method == "rules_none"  # the rules' answer stands


@respx.mock
async def test_no_answer_leaves_the_filing_pending(svc: Services) -> None:
    route = respx.post(chat_url(svc)).mock(side_effect=httpx.ConnectError("refused"))
    (unsure,) = seed(svc, [UNSURE])
    stats = await run(svc, "cascade")
    assert (stats.filings, stats.deferred, stats.stopped) == (0, 1, None)
    assert by_filing(svc, "cascade:") == {}
    with svc.engine.begin() as conn:
        failed = conn.execute(select(llm_extractions)).one()
    assert failed.status == "error" and failed.raw is None

    route.mock(side_effect=quoting_the_input)  # Ollama is back
    stats = await run(svc, "cascade")
    assert stats.llm_calls == 1 and stats.llm_cached == 0  # failures are not cached
    assert by_filing(svc, "cascade:")[unsure].method == "llm"


@respx.mock
async def test_a_dead_server_stops_the_run(svc: Services) -> None:
    respx.post(chat_url(svc)).mock(side_effect=httpx.ConnectError("refused"))
    seed(svc, [UNSURE] * 5)
    stats = await run(svc, "cascade")
    assert stats.deferred == 3 and stats.stopped and "refused" in stats.stopped
    with svc.engine.begin() as conn:
        assert len(conn.execute(select(llm_extractions)).all()) == 3


async def test_only_order_wins_have_an_extractor(svc: Services) -> None:
    with pytest.raises(ValueError, match="ORDER_WIN"):
        await run_extractions(
            svc, event_type="DIVIDEND", taxonomy_version=TAXONOMY, mode="rules", limit=1
        )
