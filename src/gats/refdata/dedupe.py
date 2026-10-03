"""Group the same disclosure filed on BSE and NSE into one event.

Most companies file each disclosure on both exchanges a few minutes apart
(median ~6 min on 2026-09 data). Counting both filings as separate events
would double-count them in every study, so filings of one security are paired
across exchanges and an event takes the earliest dissemination time.

Rules ``dedupe-v1`` (chosen on 30 days of real filings, see PROGRESS
2026-10-03; hand-checked precision 47-50 of 50):

- Candidates: same security, one BSE and one NSE filing, at most 6 h apart.
- Score = size + time + text:
  - size: +3 when both attachment sizes agree within NSE's display rounding
    (KB precision), +2 when they agree only at MB precision (two different
    multi-MB PDFs can round alike), -1 when both are known and disagree;
  - time: +2 within 15 min, +1 within 2 h;
  - text: +3 x Jaccard similarity of topic words (boilerplate and the
    company's own name removed).
- Pairs scoring >= 4 are accepted greedily, best first, each filing at most
  once. Everything else stays a single-filing event.

Changing any of this changes results: bump ``RULES_VERSION``.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection, func, select

from gats.db import repo
from gats.db.schema import announcement_event_group, announcement_security, announcements

RULES_VERSION = "dedupe-v1"
MAX_GAP = timedelta(hours=6)
MIN_SCORE = 4.0

_TOKEN = re.compile(r"[a-z]+")
# Words every filing uses (exchange boilerplate, legal references, fillers).
_STOP = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "have",
        "in",
        "is",
        "of",
        "on",
        "or",
        "the",
        "to",
        "with",
        "this",
        "that",
        "limited",
        "ltd",
        "company",
        "exchange",
        "informed",
        "regarding",
        "about",
        "under",
        "regulation",
        "regulations",
        "lodr",
        "sebi",
        "listing",
        "obligations",
        "disclosure",
        "requirements",
        "please",
        "find",
        "enclosed",
        "attached",
        "herewith",
        "refer",
        "kindly",
        "intimation",
        "update",
        "updates",
        "general",
        "others",
        "announcement",
        "pursuant",
        "submission",
        "submitted",
        "copy",
        "we",
        "hereby",
        "inform",
        "dated",
        "held",
        "no",
        "nos",
        "mr",
        "ms",
        "dr",
        "iii",
    ]
)


def topic_tokens(row: Any) -> frozenset[str]:
    """Words describing what a filing is about, comparable across exchanges."""
    if row.source == "BSE":
        # BSE subjects read "<Company> - <scrip code> - <topic>".
        parts = (row.subject or "").split(" - ")
        subject = " ".join(parts[2:]) if len(parts) >= 3 else (row.subject or "")
        text = " ".join(x or "" for x in (row.category, row.subcategory, subject, row.details))
    else:
        text = " ".join(x or "" for x in (row.category, row.details))
    words = set(_TOKEN.findall(text.lower()))
    own_name = set(_TOKEN.findall((row.company_name or "").lower()))
    return frozenset(
        w[:-1] if w.endswith("s") and len(w) > 4 else w
        for w in words - _STOP - own_name
        if len(w) > 2
    )


def size_agreement(nse_size: int | None, bse_size: int | None) -> str | None:
    """'kb' / 'mb' when sizes agree within NSE's display rounding, 'no' when
    both are known and disagree, None when either is unknown."""
    if not nse_size or not bse_size:
        return None
    mb = max(nse_size, bse_size) >= 1024**2
    unit = 1024**2 if mb else 1024 if nse_size >= 1024 else 1
    if abs(nse_size - bse_size) <= 0.005 * unit + 1:
        return "mb" if mb else "kb"
    return "no"


def pair_score(
    nse_row: Any, bse_row: Any, nse_topic: frozenset[str], bse_topic: frozenset[str]
) -> float:
    gap = abs((bse_row.event_ts - nse_row.event_ts).total_seconds())
    size = {"kb": 3.0, "mb": 2.0, "no": -1.0, None: 0.0}[
        size_agreement(nse_row.attachment_size, bse_row.attachment_size)
    ]
    timing = 2.0 if gap <= 900 else 1.0 if gap <= 7200 else 0.0
    union = nse_topic | bse_topic
    text = len(nse_topic & bse_topic) / len(union) if union else 0.0
    return size + timing + 3.0 * text


def match_security(rows: Sequence[Any]) -> list[tuple[int, int]]:
    """Accepted (nse_id, bse_id) pairs among one security's filings."""
    nse_rows = [r for r in rows if r.source == "NSE"]
    bse_rows = [r for r in rows if r.source == "BSE"]
    if not nse_rows or not bse_rows:
        return []
    topics = {r.id: topic_tokens(r) for r in rows}
    scored: list[tuple[float, float, int, int]] = []
    for n in nse_rows:
        for b in bse_rows:
            gap = abs((b.event_ts - n.event_ts).total_seconds())
            if gap > MAX_GAP.total_seconds():
                continue
            score = pair_score(n, b, topics[n.id], topics[b.id])
            if score >= MIN_SCORE:
                scored.append((score, -gap, n.id, b.id))
    scored.sort(reverse=True)
    used: set[int] = set()
    pairs = []
    for _score, _gap, n_id, b_id in scored:
        if n_id in used or b_id in used:
            continue
        used.update((n_id, b_id))
        pairs.append((n_id, b_id))
    return pairs


@dataclass
class DedupeStats:
    filings: int = 0
    pairs: int = 0
    singles: int = 0


def group_range(conn: Connection, start: datetime, end: datetime, now: datetime) -> DedupeStats:
    """(Re)group filings with ``start <= event_ts < end``.

    Filings up to ``MAX_GAP`` outside the range are read as candidates so a
    pair straddling the boundary is found from either side. The group id is
    the smaller announcement id of a pair (or the filing's own id), so it is
    deterministic and needs no allocation.
    """
    rows = conn.execute(
        select(
            announcements.c.id,
            announcements.c.source,
            announcements.c.event_ts,
            announcements.c.category,
            announcements.c.subcategory,
            announcements.c.subject,
            announcements.c.details,
            announcements.c.company_name,
            announcements.c.attachment_size,
            announcement_security.c.security_id,
        )
        .select_from(
            announcements.outerjoin(
                announcement_security,
                announcement_security.c.announcement_id == announcements.c.id,
            )
        )
        .where(
            announcements.c.event_ts >= start - MAX_GAP,
            announcements.c.event_ts < end + MAX_GAP,
        )
    ).all()
    by_security: dict[int, list[Any]] = defaultdict(list)
    for row in rows:
        if row.security_id is not None:
            by_security[row.security_id].append(row)
    group_of = {row.id: row.id for row in rows}
    paired: set[int] = set()
    stats = DedupeStats()
    for members in by_security.values():
        for n_id, b_id in match_security(members):
            group_of[n_id] = group_of[b_id] = min(n_id, b_id)
            paired.update((n_id, b_id))

    in_range = [row for row in rows if start <= row.event_ts < end]
    out = []
    for row in in_range:
        gid = group_of[row.id]
        out.append(
            {
                "announcement_id": row.id,
                "event_group_id": gid,
                "rule_version": RULES_VERSION,
                "grouped_at": now,
            }
        )
        stats.filings += 1
        stats.singles += row.id not in paired
    repo.upsert(
        conn,
        announcement_event_group,
        out,
        ["announcement_id"],
        ["event_group_id", "rule_version", "grouped_at"],
    )
    stats.pairs = (stats.filings - stats.singles) // 2  # in-range halves of pairs
    return stats


def event_groups_query() -> Any:
    """One row per event: earliest dissemination and availability, members."""
    a, g = announcements, announcement_event_group
    return (
        select(
            g.c.event_group_id,
            func.min(a.c.event_ts).label("event_ts"),
            func.min(a.c.available_at).label("available_at"),
            func.count().label("n_filings"),
            func.min(a.c.source).label("first_source"),
            func.max(a.c.source).label("last_source"),
        )
        .select_from(a.join(g, g.c.announcement_id == a.c.id))
        .group_by(g.c.event_group_id)
    )
