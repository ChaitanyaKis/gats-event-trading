"""A check on M3's yardstick: what does "buy at the open, hold, minus the
index" earn on ordinary stock-days, with no filing at all?

    .venv/Scripts/python scripts/m3_placebo.py

M3 found a negative gross abnormal return for every event type, also for
neutral ones (a management change, a shareholding report). That pattern
asks a question about the instrument before it says anything about events:
is an equal-weighted basket of liquid stocks, bought at the open and held
against the cap-weighted Nifty 500, negative on *any* day?

This is a DIAGNOSTIC, not a hypothesis test and not a trial: it has no
trading rule and passes no gate. It uses the same universe filters as the
study (EQ series, previous close at least Rs 10, 20-session median traded
value at least Rs 1 crore, not locked, opening gap under 19.5%) on every
stock-day of the period, and the same prices (NSE bhavcopy open and close,
Nifty 500 open and close). Days with a corporate action inside the window
are not adjusted here, so multi-day windows drop returns beyond +/-40%.

Read-only: it queries the database and prints a table.
"""

from __future__ import annotations

import statistics
import sys
from collections import defaultdict
from datetime import date

from sqlalchemy import select

from gats.config import Settings
from gats.db.engine import make_engine
from gats.db.schema import eod_prices, index_eod

PERIODS = {
    "train (to 2023-12-31)": (date(2019, 10, 1), date(2023, 12, 31)),
    "test (from 2024-01-01)": (date(2024, 1, 1), date(2026, 9, 30)),
}
HORIZONS = {"d0": 0, "d1": 1, "d3": 3, "d5": 5}
MIN_TURNOVER_LACS = 100.0  # Rs 1 crore
LOOKBACK = 20


def main() -> int:
    settings = Settings()
    db = make_engine(settings.resolved_db_url)
    e, ix = eod_prices, index_eod
    with db.begin() as conn:
        index = {
            row.trade_date: (row.open, row.close)
            for row in conn.execute(
                select(ix.c.trade_date, ix.c.open, ix.c.close).where(ix.c.index_name == "Nifty 500")
            )
            if row.open and row.close
        }
        rows = conn.execute(
            select(
                e.c.symbol, e.c.trade_date, e.c.prev_close, e.c.open, e.c.high, e.c.low,
                e.c.close, e.c.turnover_lacs,
            )
            .where(e.c.series == "EQ")
            .order_by(e.c.symbol, e.c.trade_date)
        )  # fmt: skip
        by_symbol: dict[str, list[tuple]] = defaultdict(list)  # type: ignore[type-arg]
        for row in rows:
            by_symbol[row.symbol].append(tuple(row))
    db.dispose()

    sessions = sorted(index)
    position = {day: i for i, day in enumerate(sessions)}
    results: dict[tuple[str, str], list[float]] = defaultdict(list)
    for days in by_symbol.values():
        closes = {d[1]: d[6] for d in days}
        for i, (_, day, prev_close, open_, high, low, _close, _turnover) in enumerate(days):
            if i < LOOKBACK or day not in position or not (open_ and prev_close and high and low):
                continue
            window = [d[7] or 0.0 for d in days[i - LOOKBACK : i]]
            if statistics.median(window) < MIN_TURNOVER_LACS or prev_close < 10 or high == low:
                continue
            if abs(open_ / prev_close - 1) >= 0.195:
                continue
            period = next((n for n, (a, b) in PERIODS.items() if a <= day <= b), None)
            if period is None:
                continue
            for name, ahead in HORIZONS.items():
                at = position[day] + ahead
                if at >= len(sessions):
                    continue
                exit_day = sessions[at]
                exit_close = closes.get(exit_day)
                if not exit_close:
                    continue
                stock = exit_close / open_ - 1
                if ahead and abs(stock) > 0.40:
                    continue  # an unadjusted split or bonus inside the window
                market = index[exit_day][1] / index[day][0] - 1
                results[(period, name)].append(stock - market)

    print("Ordinary stock-days, bought at the open, market-adjusted (gross, no costs):")
    print(
        f"{'period':<24} {'exit':<4} {'stock-days':>11} {'mean':>8} {'median':>8} {'share > 0':>10}"
    )
    for (period, name), values in sorted(results.items()):
        mean = sum(values) / len(values)
        positive = sum(v > 0 for v in values) / len(values)
        print(
            f"{period:<24} {name:<4} {len(values):>11,} {mean:>+8.2%} "
            f"{statistics.median(values):>+8.2%} {positive:>10.2f}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
