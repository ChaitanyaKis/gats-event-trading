"""Guards around a real study run: data readiness and the holdout log.

The test period is used once. Two things make that enforceable rather than
a promise:

- :func:`data_readiness` refuses to run on incomplete data (a run on a
  partial backfill would spend the holdout on the wrong sample);
- every real run is appended to ``<data>/research/<study>/runs.jsonl`` with
  the config hash, so a second run of the same design is flagged as a
  reproduction, never silently treated as a fresh test.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, func, select

from gats.db.schema import backfill_days, eod_prices, index_eod
from gats.refdata.calendar import TradingCalendar
from gats.research.study import StudyConfig
from gats.timeutil import utcnow

MIN_COVERAGE = 0.98


@dataclass
class Readiness:
    sessions: int
    eod_share: float
    index_share: float
    filings_share: float
    missing_eod: list[date] = field(default_factory=list)
    missing_filing_days: list[date] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return min(self.eod_share, self.index_share, self.filings_share) >= MIN_COVERAGE

    def problems(self) -> list[str]:
        out = []
        for label, share in (
            ("sessions with stock prices", self.eod_share),
            ("sessions with index closes", self.index_share),
            ("days with NSE filings backfilled", self.filings_share),
        ):
            if share < MIN_COVERAGE:
                out.append(f"{label}: {share:.1%} (need {MIN_COVERAGE:.0%})")
        return out


def data_readiness(conn: Connection, cfg: StudyConfig, cal: TradingCalendar) -> Readiness:
    expected = cal.trading_days(cfg.data.start, cfg.data.end, include_special=False)
    eod_days = set(
        conn.execute(
            select(eod_prices.c.trade_date)
            .where(eod_prices.c.trade_date.between(cfg.data.start, cfg.data.end))
            .distinct()
        ).scalars()
    )
    index_days = set(
        conn.execute(
            select(index_eod.c.trade_date)
            .where(
                index_eod.c.trade_date.between(cfg.data.start, cfg.data.end),
                index_eod.c.index_name == cfg.benchmark.index,
            )
            .distinct()
        ).scalars()
    )
    calendar_days = (cfg.data.end - cfg.data.start).days + 1
    complete = set(
        conn.execute(
            select(backfill_days.c.day).where(
                backfill_days.c.source == cfg.data.source,
                backfill_days.c.status == "complete",
                backfill_days.c.day.between(cfg.data.start, cfg.data.end),
            )
        ).scalars()
    )
    n = len(expected) or 1
    all_days = [date.fromordinal(cfg.data.start.toordinal() + k) for k in range(calendar_days)]
    return Readiness(
        sessions=len(expected),
        eod_share=sum(d in eod_days for d in expected) / n,
        index_share=sum(d in index_days for d in expected) / n,
        filings_share=len(complete) / calendar_days if calendar_days else 0.0,
        missing_eod=[d for d in expected if d not in eod_days][:20],
        missing_filing_days=[d for d in all_days if d not in complete][:20],
    )


def run_log_path(data_dir: Path, study: str) -> Path:
    return data_dir / "research" / study / "runs.jsonl"


def previous_runs(data_dir: Path, study: str, config_hash: str) -> list[dict[str, Any]]:
    path = run_log_path(data_dir, study)
    if not path.exists():
        return []
    runs = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    return [r for r in runs if r.get("config_hash") == config_hash]


def log_run(data_dir: Path, study: str, entry: dict[str, Any]) -> None:
    path = run_log_path(data_dir, study)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, default=str) + "\n")


def database_fingerprint(conn: Connection) -> dict[str, Any]:
    """Row counts and ranges that identify the data a run saw."""
    eod_n, eod_min, eod_max = conn.execute(
        select(func.count(), func.min(eod_prices.c.trade_date), func.max(eod_prices.c.trade_date))
    ).one()
    return {"eod_rows": int(eod_n), "eod_first": eod_min, "eod_last": eod_max}


def stamp() -> str:
    """A run id: the UTC time, sortable."""
    return utcnow().strftime("%Y%m%dT%H%M%SZ")
