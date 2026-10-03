"""Human labels for extraction (T4.5): the sample, the review step, the file.

Ground truth comes from a person reading the filing. Three choices keep it
honest:

- **Held out.** The sample comes from filings nobody read while the rules
  were written (the rules were built on 2026-09 filings). Labels on the
  development texts would grade the rules on their own homework.
- **Fixed in advance.** :func:`draw_sample` draws a seeded sample,
  stratified by how the rules read each filing (annexure, sentence,
  nothing), and the sample file is committed before any labelling.
- **Anchoring measured, not ignored.** Checking a proposal is faster than
  labelling from scratch but pulls answers toward the proposal. Each item
  shows the rules' or the LLM's proposal at random (half each, recorded),
  so T4.6 can compare each method on the items where it was *not* shown.

Labels go to a JSONL file one decision at a time (the latest line per
document wins), so quitting loses nothing.
"""

from __future__ import annotations

import json
import random
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError
from sqlalchemy import Connection, and_, select

from gats.db.schema import announcement_event_types, announcements, document_texts
from gats.extract.cascade import code_hash, extractor_version, llm_answer
from gats.extract.llm import prompt_hash
from gats.extract.money import find_amounts, parse_amount
from gats.extract.pdf_text import EXTRACTOR, EXTRACTOR_VERSION
from gats.extract.rules import RULES_VERSION, annexure_fields, duration_months, extract_order_win
from gats.extract.schemas import OrderWin
from gats.extract.texts import document_text
from gats.ingest import Services
from gats.timeutil import ist_datetime, to_ist

TARGET_LABELS = 300  # ROADMAP T4.5

GUIDE_VERSION = "label-guide-v1"
KINDS = ("new_order", "amendment", "lowest_bidder", "other")
PROPOSERS = ("rules", "llm")
LABEL_FIELDS = (
    "amount_text",
    "counterparty",
    "domestic_or_export",
    "duration_months",
    "is_repeat_order",
)


@dataclass(frozen=True)
class SampleItem:
    doc_id: str
    source: str
    source_ann_id: str
    symbol: str | None
    filed: date
    stratum: str  # how the rules read it: annexure | sentence | none
    shown: str  # whose proposal the labeller sees: rules | llm


@dataclass
class Sample:
    name: str
    event_type: str
    taxonomy_version: str
    start: date
    end: date
    source: str | None
    seed: int
    population: int
    strata: dict[str, dict[str, int]]
    rules_version: str
    created_at: datetime
    items: list[SampleItem]

    def save(self, path: Path) -> None:
        """One item per line, so diffs and reviews stay readable."""
        head = asdict(self)
        items = head.pop("items")
        body = json.dumps(head, indent=1, default=str, ensure_ascii=False)
        lines = ",\n".join("  " + json.dumps(i, default=str, ensure_ascii=False) for i in items)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body[:-2] + ',\n "items": [\n' + lines + "\n ]\n}\n", "utf-8", newline="\n")

    @classmethod
    def load(cls, path: Path) -> Sample:
        data = json.loads(path.read_text(encoding="utf-8"))
        items = [
            SampleItem(**(i | {"filed": date.fromisoformat(i["filed"])})) for i in data.pop("items")
        ]
        return cls(
            **(
                data
                | {
                    "start": date.fromisoformat(data["start"]),
                    "end": date.fromisoformat(data["end"]),
                    "created_at": datetime.fromisoformat(data["created_at"]),
                    "items": items,
                }
            )
        )


@dataclass(frozen=True)
class Candidate:
    announcement_id: int
    doc_id: str
    source: str
    source_ann_id: str
    symbol: str | None
    filed: date
    text: str


def candidates(
    conn: Connection,
    *,
    event_type: str,
    taxonomy_version: str,
    start: date,
    end: date,
    source: str | None,
) -> list[Candidate]:
    """Filings of the type filed on IST dates [start, end] whose attachment
    has text, one per document: the same PDF filed on both exchanges is one
    item (the earlier filing)."""
    a, et, t = announcements, announcement_event_types, document_texts
    query = (
        select(
            a.c.id,
            a.c.attachment_doc_id,
            a.c.source,
            a.c.source_ann_id,
            a.c.symbol,
            a.c.event_ts,
            t.c.text,
        )
        .join(
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
                t.c.needs_ocr.is_(False),
            ),
        )
        .where(
            a.c.event_ts >= ist_datetime(start, time()),
            a.c.event_ts < ist_datetime(end + timedelta(days=1), time()),
        )
        .order_by(a.c.event_ts, a.c.id)
    )
    if source is not None:
        query = query.where(a.c.source == source)
    seen: set[str] = set()
    found = []
    for row in conn.execute(query):
        if row.attachment_doc_id in seen or not row.text.strip():
            continue
        seen.add(row.attachment_doc_id)
        found.append(
            Candidate(
                announcement_id=row.id,
                doc_id=row.attachment_doc_id,
                source=row.source,
                source_ann_id=row.source_ann_id,
                symbol=row.symbol,
                filed=to_ist(row.event_ts).date(),
                text=row.text,
            )
        )
    return found


def allocate(sizes: Mapping[str, int], n: int) -> dict[str, int]:
    """Proportional allocation by largest remainder (ties by name), so the
    strata add up to exactly ``n``."""
    total = sum(sizes.values())
    if n >= total:
        return dict(sizes)
    exact = {k: n * v / total for k, v in sizes.items()}
    alloc = {k: int(x) for k, x in exact.items()}
    by_remainder = sorted(exact, key=lambda k: (alloc[k] - exact[k], k))
    for k in by_remainder[: n - sum(alloc.values())]:
        alloc[k] += 1
    return alloc


def draw_sample(
    conn: Connection,
    *,
    name: str,
    event_type: str,
    taxonomy_version: str,
    start: date,
    end: date,
    source: str | None,
    n: int,
    seed: int,
    now: datetime,
) -> Sample:
    if event_type != "ORDER_WIN":
        raise ValueError(f"no extractor for {event_type} yet (M4 scope is ORDER_WIN)")
    pool = candidates(
        conn,
        event_type=event_type,
        taxonomy_version=taxonomy_version,
        start=start,
        end=end,
        source=source,
    )
    strata: dict[str, list[Candidate]] = {}
    for candidate in pool:
        method = extract_order_win(candidate.text, filed=candidate.filed).method
        strata.setdefault(method, []).append(candidate)
    alloc = allocate({k: len(v) for k, v in strata.items()}, n)
    rng = random.Random(seed)
    chosen = [(k, c) for k in sorted(strata) for c in rng.sample(strata[k], alloc[k])]
    rng.shuffle(chosen)  # any prefix is a fair sample if labelling stops early
    shown = [PROPOSERS[i % 2] for i in range(len(chosen))]
    rng.shuffle(shown)
    items = [
        SampleItem(c.doc_id, c.source, c.source_ann_id, c.symbol, c.filed, stratum, who)
        for (stratum, c), who in zip(chosen, shown, strict=True)
    ]
    return Sample(
        name=name,
        event_type=event_type,
        taxonomy_version=taxonomy_version,
        start=start,
        end=end,
        source=source,
        seed=seed,
        population=len(pool),
        strata={k: {"population": len(strata[k]), "sample": alloc[k]} for k in sorted(strata)},
        rules_version=RULES_VERSION,
        created_at=now,
        items=items,
    )


# --- the labels file ----------------------------------------------------------------


def load_labels(path: Path) -> dict[str, dict[str, Any]]:
    """doc_id -> its latest label record."""
    labels: dict[str, dict[str, Any]] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = json.loads(line)
                labels[record["doc_id"]] = record
    return labels


def append_label(path: Path, record: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as out:
        out.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


# --- the review step ----------------------------------------------------------------


class UI(Protocol):
    def show(self, text: str = "", *, highlight: bool = False) -> None: ...

    def ask(self, prompt: str, *, default: str | None = None) -> str: ...

    def page(self, text: str) -> None: ...


@dataclass(frozen=True)
class Proposal:
    order: OrderWin | None
    by: str  # rules | llm
    note: str  # e.g. "annexure", "ok", "not_in_text"
    version: str


@dataclass(frozen=True)
class ReviewItem:
    item: SampleItem
    text: str
    summary: str  # company, subject and the exchange's description
    proposal: Proposal


def rupees(value: float) -> str:
    if value >= 1e7:
        return f"Rs {value / 1e7:,.2f} crore"
    if value >= 1e5:
        return f"Rs {value / 1e5:,.2f} lakh"
    return f"Rs {value:,.2f}"


def amounts_in_context(text: str, width: int = 90) -> list[tuple[str, str, str]]:
    """(before, amount, after) for every amount in the text, in order."""
    flat = " ".join(text.split())
    found = []
    cursor = 0
    for money in find_amounts(flat):
        at = flat.find(money.text, cursor)
        if at < 0:
            continue
        cursor = at + len(money.text)
        found.append((flat[max(0, at - width) : at], money.text, flat[cursor : cursor + width]))
    return found


def _describe(order: OrderWin | None) -> list[str]:
    if order is None:
        return ["  (no proposal: label from the text)"]
    value = ""
    if order.amount_inr is not None:
        value = f"  = {rupees(order.amount_inr)}"
    elif order.amount is not None:
        value = f"  = {order.currency} {order.amount:,.0f}"
    return [
        f"  amount_text:        {order.amount_text or '-'}{value}",
        f"  counterparty:       {order.counterparty or '-'}",
        f"  domestic_or_export: {order.domestic_or_export}",
        f"  duration_months:    {order.duration_months if order.duration_months else '-'}",
        f"  is_repeat_order:    {_yes_no(order.is_repeat_order)}",
    ]


def _yes_no(value: bool | None) -> str:
    return "-" if value is None else ("yes" if value else "no")


def _show(ui: UI, entry: ReviewItem, position: str, max_amounts: int = 12) -> None:
    item = entry.item
    ui.show()
    ui.show(
        f"[{position}] {item.source} {item.symbol or ''} | filed {item.filed} | "
        f"doc {item.doc_id[:10]}",
        highlight=True,
    )
    ui.show(entry.summary)
    fields = annexure_fields(entry.text)
    if fields:
        ui.show("Annexure as the rules read it:")
        for name in ("counterparty", "dom_intl", "period", "size"):
            if name in fields:
                ui.show(f"  {name}: {fields[name][:160]}")
    found = amounts_in_context(entry.text)
    ui.show(f"Amounts in the text ({len(found)}):")
    for before, amount, after in found[:max_amounts]:
        ui.show(f"  ...{before[-70:]} [{amount}] {after[:70]}...")
    if len(found) > max_amounts:
        ui.show(f"  ... {len(found) - max_amounts} more ('t' shows the full text)")
    proposal = entry.proposal
    ui.show(f"Proposal ({proposal.by}: {proposal.note}):", highlight=True)
    for line in _describe(proposal.order):
        ui.show(line)


def _ask_choice(ui: UI, prompt: str, choices: Sequence[str], default: str | None) -> str:
    while True:
        answer = ui.ask(f"{prompt} [{'/'.join(choices)}]", default=default).strip().lower()
        matches = [c for c in choices if c.startswith(answer)] if answer else []
        if len(matches) == 1:
            return matches[0]
        ui.show(f"  choose one of: {', '.join(choices)}")


def _ask_fields(ui: UI, base: OrderWin | None, filed: date) -> OrderWin:
    """Field by field, the proposal as default; numbers derived as always."""
    while True:
        amount_text = ui.ask(
            "amount_text, as written ('-' = not disclosed)",
            default=(base.amount_text if base and base.amount_text else "-"),
        ).strip()
        if amount_text != "-" and parse_amount(amount_text) is None:
            ui.show("  not an amount; write it like 'Rs. 72.77 crore' or 'USD 1.2 million'")
            continue
        break
    counterparty = ui.ask(
        "counterparty ('-' = not disclosed)",
        default=(base.counterparty if base and base.counterparty else "-"),
    ).strip()
    where = _ask_choice(
        ui,
        "domestic_or_export",
        ("domestic", "export", "unknown"),
        base.domestic_or_export if base else "unknown",
    )
    while True:
        raw = ui.ask(
            "duration_months: months, '2 years', an end date like 31.03.2027, or '-'",
            default=str(base.duration_months) if base and base.duration_months else "-",
        ).strip()
        if raw == "-":
            months = None
            break
        try:
            months = float(raw)
        except ValueError:
            months = duration_months(raw, filed)  # the same reading as the extractors
        if months is not None and 0 < months <= 600:
            break
        ui.show("  not a duration between 0 and 600 months")
    repeat = _ask_choice(
        ui, "is_repeat_order", ("yes", "no", "-"), _yes_no(base.is_repeat_order if base else None)
    )
    try:
        return OrderWin(
            amount_text=None if amount_text == "-" else amount_text,
            counterparty=None if counterparty == "-" else counterparty,
            domestic_or_export=where,  # type: ignore[arg-type]
            duration_months=months,
            is_repeat_order=None if repeat == "-" else repeat == "yes",
            confidence=1.0,
        )
    except ValidationError as exc:  # e.g. an over-long counterparty
        ui.show(f"  invalid: {exc.errors()[0]['msg']}; try again")
        return _ask_fields(ui, base, filed)


def review_one(
    ui: UI, entry: ReviewItem, position: str, now: Callable[[], datetime]
) -> dict[str, Any] | None:
    """Show one filing and record the decision; None means quit."""
    item, proposal = entry.item, entry.proposal
    _show(ui, entry, position)
    actions: tuple[str, ...] = ("accept", "edit", "not-order", "skip", "text", "quit")
    if proposal.order is None:
        actions = tuple(a for a in actions if a != "accept")
    while True:
        action = _ask_choice(ui, "decision", actions, None)
        if action == "text":
            ui.page(entry.text)
            continue
        break
    if action == "quit":
        return None
    record: dict[str, Any] = {
        "doc_id": item.doc_id,
        "source": item.source,
        "source_ann_id": item.source_ann_id,
        "symbol": item.symbol,
        "filed": item.filed.isoformat(),
        "status": "labelled",
        "decision": action,
        "kind": "new_order",
        "fields": None,
        "amount_inr": None,
        "note": "",
        "shown": proposal.by,
        "proposal": _fields(proposal.order),
        "proposal_version": proposal.version,
        "guide": GUIDE_VERSION,
    }
    order: OrderWin | None = None
    if action == "accept":
        order = proposal.order
    elif action == "edit":
        record["kind"] = _ask_choice(ui, "kind", KINDS, "new_order")
        order = _ask_fields(ui, proposal.order, item.filed)
    elif action == "not-order":
        # Amendments go through edit: they may state a revised amount.
        record["kind"] = _ask_choice(ui, "kind", ("lowest_bidder", "other"), None)
    else:
        record["status"], record["kind"] = "skipped", None
    if order is not None:
        record["fields"] = _fields(order)
        record["amount_inr"] = order.amount_inr
    record["note"] = ui.ask("note (optional)", default="").strip()
    record["labelled_at"] = now().isoformat()
    return record


def _fields(order: OrderWin | None) -> dict[str, Any] | None:
    if order is None:
        return None
    dumped = order.model_dump()
    return {name: dumped[name] for name in (*LABEL_FIELDS, "amount", "currency", "amount_inr")}


# --- driving it from the database ---------------------------------------------------


def resolve(conn: Connection, sample: Sample) -> dict[str, int]:
    """doc_id -> this database's announcement id for each sampled filing (ids
    are local; the sample names filings by exchange id)."""
    a = announcements
    keys = {(i.source, i.source_ann_id): i.doc_id for i in sample.items}
    rows = conn.execute(
        select(a.c.id, a.c.source, a.c.source_ann_id).where(
            a.c.source_ann_id.in_(sorted({k[1] for k in keys}))
        )
    )
    return {
        keys[(r.source, r.source_ann_id)]: int(r.id)
        for r in rows
        if (r.source, r.source_ann_id) in keys
    }


async def review_entry(
    svc: Services, item: SampleItem, announcement_id: int | None, *, llm_ok: bool = True
) -> ReviewItem | None:
    """Text, summary and proposal for one item. None when the item needs the
    LLM and it gives no answer (or ``llm_ok`` is False): the item stays
    pending rather than being shown without its assigned proposal."""
    a = announcements
    with svc.engine.begin() as conn:
        text = document_text(conn, item.doc_id) or ""
        row = (
            conn.execute(
                select(a.c.company_name, a.c.subject, a.c.details).where(a.c.id == announcement_id)
            ).first()
            if announcement_id is not None
            else None
        )
    summary = ""
    if row is not None:
        summary = " | ".join(p for p in (row.company_name, row.subject) if p)
        if row.details:
            details = " ".join(str(row.details).split())
            summary += "\n" + details[:400]
    if item.shown == "rules":
        ruled = extract_order_win(text, filed=item.filed)
        proposal = Proposal(
            ruled.order, "rules", ruled.method, f"rules:{RULES_VERSION}+{code_hash()}"
        )
    else:
        if not llm_ok:
            return None
        result, _ = await llm_answer(svc, item.doc_id, text, item.filed)
        if result.status == "error":
            return None
        settings = svc.settings
        version = extractor_version("llm", prompt_hash(settings.llm_max_chars), settings.llm_model)
        proposal = Proposal(result.order, "llm", result.status, version)
    return ReviewItem(item, text, summary, proposal)


def summarize(sample: Sample, labels: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Progress and decisions, for ``gats label stats``."""
    mine = [labels[i.doc_id] for i in sample.items if i.doc_id in labels]
    labelled = [r for r in mine if r["status"] == "labelled"]
    lines = [
        f"{len(labelled)} labelled, {len(mine) - len(labelled)} skipped, "
        f"{len(sample.items) - len(mine)} to go (target {TARGET_LABELS})",
        f"  decisions: {dict(Counter(r['decision'] for r in mine))}",
        f"  kinds: {dict(Counter(r['kind'] for r in labelled))}",
    ]
    for who in PROPOSERS:
        shown = [r for r in labelled if r["shown"] == who]
        accepted = sum(r["decision"] == "accept" for r in shown)
        if shown:
            lines.append(
                f"  {who} proposals accepted as shown: {accepted}/{len(shown)} "
                f"({accepted / len(shown):.0%})"
            )
    return lines
