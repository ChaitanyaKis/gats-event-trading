"""What a paper run has done so far, read from the database (T7.1).

Nothing here replays the engine: these are the stored journal, orders and
fills, so the numbers can be shown while the run is live in another
process, and for a run whose design the current code no longer matches.

The two latencies are what T5.3 (the intraday gate) and T7.2 (the latency
budget) need measured rather than assumed:

- **feed**: from the exchange's dissemination time to the recorder first
  seeing the filing (dominated by the poll interval);
- **hand-over**: from there to the strategy being handed the event (the
  attachment, its text, the facts, the first price).
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, Row, func, select

from gats.db.schema import (
    announcements,
    paper_executions,
    paper_journal,
    paper_orders,
    paper_runs,
)


@dataclass(frozen=True)
class Latency:
    n: int
    median_s: float
    p95_s: float
    worst_s: float


def latency(seconds: Sequence[float]) -> Latency | None:
    """Median, 95th percentile (nearest rank) and worst of a sample."""
    if not seconds:
        return None
    ordered = sorted(seconds)

    def rank(q: float) -> float:
        return ordered[max(0, math.ceil(q * len(ordered)) - 1)]

    return Latency(len(ordered), rank(0.5), rank(0.95), ordered[-1])


@dataclass
class RunStatus:
    name: str
    strategy: str
    design_hash: str
    created_at: datetime
    steps: Counter[str] = field(default_factory=Counter)  # journal rows by kind
    last_step_at: datetime | None = None
    orders: Counter[str] = field(default_factory=Counter)  # by status
    refusals: Counter[str] = field(default_factory=Counter)  # rejected orders by reason
    fills: int = 0
    bought: float = 0.0  # rupees
    sold: float = 0.0
    charges: float = 0.0
    open_quantity: dict[str, int] = field(default_factory=dict)  # instrument -> shares held
    feed: Latency | None = None
    hand_over: Latency | None = None


def runs(conn: Connection) -> list[Row[Any]]:
    return list(conn.execute(select(paper_runs).order_by(paper_runs.c.id)).all())


def _reason(note: str) -> str:
    """A rejection note without its numbers, so equal reasons count together."""
    return note.split(":", 2)[1].strip() if note.startswith("risk:") else note.split(":")[0]


def run_status(conn: Connection, name: str) -> RunStatus | None:
    run = conn.execute(select(paper_runs).where(paper_runs.c.name == name)).first()
    if run is None:
        return None
    status = RunStatus(run.name, run.strategy, run.design_hash, run.created_at)
    j, o, e = paper_journal, paper_orders, paper_executions
    for kind, n in conn.execute(
        select(j.c.kind, func.count()).where(j.c.run_id == run.id).group_by(j.c.kind)
    ):
        status.steps[kind] = int(n)
    status.last_step_at = conn.execute(
        select(j.c.at).where(j.c.run_id == run.id).order_by(j.c.seq.desc()).limit(1)
    ).scalar()
    for row in conn.execute(select(o.c.status, o.c.note).where(o.c.run_id == run.id)):
        status.orders[row.status] += 1
        if row.status == "rejected":
            status.refusals[_reason(row.note)] += 1
    held: Counter[str] = Counter()
    for row in conn.execute(select(e).where(e.c.run_id == run.id).order_by(e.c.seq)):
        status.fills += 1
        status.charges += row.charges
        value = row.quantity * row.price
        if row.side == "buy":
            status.bought += value
            held[row.instrument_key] += row.quantity
        else:
            status.sold += value
            held[row.instrument_key] -= row.quantity
    status.open_quantity = {key: n for key, n in held.items() if n}

    events = conn.execute(
        select(j.c.at, j.c.payload).where(j.c.run_id == run.id, j.c.kind == "event")
    ).all()
    handed = {int(row.payload["event_id"]): row for row in events}
    status.hand_over = latency(
        [
            (row.at - datetime.fromisoformat(row.payload["available_at"])).total_seconds()
            for row in handed.values()
        ]
    )
    a = announcements
    seen = (
        conn.execute(
            select(a.c.available_at, a.c.exch_disseminated_ts).where(
                a.c.id.in_(sorted(handed)),
                a.c.ingest_mode == "live",  # a catch-up after downtime says nothing of the feed
                a.c.exch_disseminated_ts.is_not(None),
            )
        ).all()
        if handed
        else []
    )
    status.feed = latency(
        [(row.available_at - row.exch_disseminated_ts).total_seconds() for row in seen]
    )
    return status
