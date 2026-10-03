"""Rules first, LLM when the rules are unsure (T4.4).

Rules are free, instant and exact when the SEBI annexure is present; the LLM
costs ~15 s a filing on the laptop GPU and can be wrong in new ways. So the
cascade asks the LLM only when the rules are unsure, and keeps the rules'
answer if the LLM's answer fails validation. Modes ``rules`` and ``llm``
exist so T4.6 can compare all three on the same labelled filings.

Results go to ``extractions`` under a version string naming every
component (rules version, prompt hash, model, mode) plus a hash of the
extraction code itself, so changing any of them, even by a regex, extracts
again into new rows instead of leaving stale ones under a current name.

A call that gets no answer (Ollama down, a timeout) decides nothing: the
filing stays pending for the next run. Three such failures in a row stop
the run rather than wait out every filing's retries against a dead server.
"""

from __future__ import annotations

import hashlib
import inspect
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import Connection, and_, select

from gats.db import repo
from gats.db.schema import (
    announcement_event_types,
    announcements,
    document_texts,
    extractions,
    llm_extractions,
)
from gats.extract import llm, money, rules, schemas
from gats.extract.llm import (
    LlmResult,
    llm_order_win,
    model_input,
    prompt_hash,
    validate_response,
)
from gats.extract.pdf_text import EXTRACTOR, EXTRACTOR_VERSION
from gats.extract.rules import RULES_VERSION, extract_order_win
from gats.extract.schemas import OrderWin
from gats.ingest import Services
from gats.timeutil import to_ist

Mode = Literal["rules", "llm", "cascade"]
_MAX_CONSECUTIVE_FAILURES = 3


@cache
def code_hash() -> str:
    """Hash of the code that turns text into facts (line endings normalised,
    so Windows and Linux checkouts agree)."""
    digest = hashlib.sha256()
    for module in (money, schemas, rules, llm):
        digest.update(Path(inspect.getfile(module)).read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()[:8]


def extractor_version(mode: Mode, prompt: str, model: str) -> str:
    code = code_hash()
    if mode == "rules":
        return f"rules:{RULES_VERSION}+{code}"
    if mode == "llm":
        return f"llm:{prompt}:{model}+{code}"
    return f"cascade:{RULES_VERSION}:{prompt}:{model}+{code}"


@dataclass
class CascadeStats:
    version: str
    filings: int = 0
    by_method: Counter[str] = field(default_factory=Counter)
    llm_status: Counter[str] = field(default_factory=Counter)
    llm_calls: int = 0
    llm_cached: int = 0
    llm_ms: int = 0
    deferred: int = 0  # no LLM answer: left pending
    stopped: str | None = None  # why the run stopped early


def _pending(
    conn: Connection, event_type: str, taxonomy_version: str, version: str, limit: int
) -> list[Any]:
    a, et, t, done = announcements, announcement_event_types, document_texts, extractions
    return list(
        conn.execute(
            select(a.c.id, a.c.attachment_doc_id, a.c.event_ts, t.c.text)
            .select_from(
                a.join(
                    et,
                    and_(
                        et.c.announcement_id == a.c.id,
                        et.c.taxonomy_version == taxonomy_version,
                        et.c.event_type == event_type,
                    ),
                )
                .join(
                    t,
                    and_(
                        t.c.doc_id == a.c.attachment_doc_id,
                        t.c.extractor == EXTRACTOR,
                        t.c.extractor_version == EXTRACTOR_VERSION,
                        t.c.error.is_(None),
                    ),
                )
                .outerjoin(
                    done,
                    and_(done.c.announcement_id == a.c.id, done.c.extractor_version == version),
                )
            )
            .where(done.c.announcement_id.is_(None))
            .order_by(a.c.event_ts.desc())
            .limit(limit)
        ).all()
    )


def _cached(conn: Connection, doc_id: str, event_type: str, prompt: str, model: str) -> str | None:
    """The model's earlier reply to this input. Failed calls left no reply,
    so they are retried."""
    row = conn.execute(
        select(llm_extractions.c.raw).where(
            llm_extractions.c.doc_id == doc_id,
            llm_extractions.c.event_type == event_type,
            llm_extractions.c.prompt_hash == prompt,
            llm_extractions.c.model == model,
            llm_extractions.c.raw.is_not(None),
        )
    ).first()
    return None if row is None else str(row.raw)


def _store_llm(
    conn: Connection,
    doc_id: str,
    event_type: str,
    prompt: str,
    model: str,
    result: LlmResult,
    now: datetime,
) -> None:
    repo.upsert(
        conn,
        llm_extractions,
        [
            {
                "doc_id": doc_id,
                "event_type": event_type,
                "prompt_hash": prompt,
                "model": model,
                "status": result.status,
                "output": result.order.model_dump() if result.order else None,
                "raw": result.raw,
                "error": result.error,
                "latency_ms": result.latency_ms,
                "prompt_tokens": result.prompt_tokens,
                "output_tokens": result.output_tokens,
                "created_at": now,
            }
        ],
        ["doc_id", "event_type", "prompt_hash", "model"],
        [
            "status",
            "output",
            "raw",
            "error",
            "latency_ms",
            "prompt_tokens",
            "output_tokens",
            "created_at",
        ],
    )


async def run_extractions(
    svc: Services, *, event_type: str, taxonomy_version: str, mode: Mode, limit: int
) -> CascadeStats:
    """Extract facts for up to ``limit`` filings not yet extracted by this version."""
    if event_type != "ORDER_WIN":
        raise ValueError(f"no extractor for {event_type} yet (M4 scope is ORDER_WIN)")
    settings = svc.settings
    prompt, model = prompt_hash(settings.llm_max_chars), settings.llm_model
    version = extractor_version(mode, prompt, model)
    stats = CascadeStats(version)
    with svc.engine.begin() as conn:
        rows = _pending(conn, event_type, taxonomy_version, version, limit)
    failures = 0
    for row in rows:
        filed = to_ist(row.event_ts).date()
        order: OrderWin | None = None
        method = "none"
        unsure = True
        if mode in ("rules", "cascade"):
            ruled = extract_order_win(row.text, filed=filed)
            order, method, unsure = ruled.order, f"rules_{ruled.method}", ruled.unsure
        if mode == "llm" or (mode == "cascade" and unsure):
            with svc.engine.begin() as conn:
                reply = _cached(conn, row.attachment_doc_id, event_type, prompt, model)
            result: LlmResult
            if reply is None:
                result = await llm_order_win(svc.client, settings, row.text, filed)
                stats.llm_calls += 1
                stats.llm_ms += result.latency_ms or 0
                with svc.engine.begin() as conn:
                    _store_llm(
                        conn, row.attachment_doc_id, event_type, prompt, model, result, svc.clock()
                    )
            else:
                result = validate_response(reply, model_input(row.text, settings), filed)
                stats.llm_cached += 1
            stats.llm_status[result.status] += 1
            if result.status == "error":
                stats.deferred += 1
                failures += 1
                if failures >= _MAX_CONSECUTIVE_FAILURES:
                    stats.stopped = f"{failures} LLM calls failed in a row: {result.error}"
                    break
                continue
            failures = 0
            if result.status == "ok" and result.order is not None:
                order, method = result.order, "llm"
            elif mode == "llm":
                order, method = None, f"llm_{result.status}"
        stats.filings += 1
        stats.by_method[method] += 1
        with svc.engine.begin() as conn:
            repo.upsert(
                conn,
                extractions,
                [
                    {
                        "announcement_id": row.id,
                        "extractor_version": version,
                        "event_type": event_type,
                        "method": method,
                        "fields": order.model_dump() if order else None,
                        "amount_inr": order.amount_inr if order else None,
                        "confidence": order.confidence if order else 0.0,
                        "created_at": svc.clock(),
                    }
                ],
                ["announcement_id", "extractor_version"],
                ["method", "fields", "amount_inr", "confidence", "created_at"],
            )
    return stats
