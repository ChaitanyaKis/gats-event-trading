"""Is the ~0.1% same-day loss on ordinary stock-days (scripts/m3_placebo.py)
a real effect, or a mismatch in how the stock's and the index's "open" are
measured?

    .venv/Scripts/python scripts/open_measure_check.py

A stock's return from open to close uses the bhavcopy OPEN_PRICE (the
pre-open auction price). The index's uses the "Open" of NSE's index file. If
that index open is computed from stale prices (stocks that have not traded
yet count at their previous close), it sits too near yesterday's close: part
of the overnight move is then booked as intraday for the index, and every
stock looks worse than the index from the open.

Three checks, on the same liquid universe as the placebo:

1. Split each day's close-to-close return into overnight (previous close to
   open) and intraday (open to close), for stocks and for the index. Close
   to close cannot be mismeasured; if stocks and index agree there and
   disagree on the split, the split is the problem.
2. Regress the index's overnight move on the stocks' average overnight move
   across days. A properly measured index open moves one for one with its
   stocks' opens (slope near 1); a stale one moves much less.
3. The same against the Nifty 50, whose stocks all open at once.

A DIAGNOSTIC: no trading rule, no gate, not a trial. Read-only.
"""

from __future__ import annotations

import statistics
import sys
from collections import defaultdict
from datetime import date
from itertools import pairwise

from sqlalchemy import select

from gats.config import Settings
from gats.db.engine import make_engine
from gats.db.schema import eod_prices, index_eod

START, END = date(2019, 10, 1), date(2026, 9, 30)
MIN_TURNOVER_LACS, LOOKBACK = 100.0, 20
INDEXES = ("Nifty 500", "Nifty 50")


def slope(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """OLS slope of y on x, and their correlation."""
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    return sxy / sxx, sxy / (sxx * syy) ** 0.5


def main() -> int:
    db = make_engine(Settings().resolved_db_url)
    e, ix = eod_prices, index_eod
    with db.begin() as conn:
        index: dict[str, dict[date, tuple[float, float]]] = {name: {} for name in INDEXES}
        for row in conn.execute(
            select(ix.c.index_name, ix.c.trade_date, ix.c.open, ix.c.close).where(
                ix.c.index_name.in_(INDEXES)
            )
        ):
            if row.open and row.close:
                index[row.index_name][row.trade_date] = (row.open, row.close)
        by_symbol: dict[str, list[tuple]] = defaultdict(list)  # type: ignore[type-arg]
        for row in conn.execute(
            select(
                e.c.symbol, e.c.trade_date, e.c.prev_close, e.c.open, e.c.high, e.c.low,
                e.c.close, e.c.turnover_lacs,
            )
            .where(e.c.series == "EQ", e.c.trade_date >= START, e.c.trade_date <= END)
            .order_by(e.c.symbol, e.c.trade_date)
        ):  # fmt: skip
            by_symbol[row.symbol].append(tuple(row))
    db.dispose()

    # Per day: equal- and turnover-weighted sums of the stocks' overnight,
    # intraday and close-to-close returns.
    day_sums: dict[date, list[float]] = defaultdict(lambda: [0.0] * 8)
    for days in by_symbol.values():
        for i, (_, day, prev_close, open_, high, low, close, _turnover) in enumerate(days):
            if i < LOOKBACK or not (open_ and prev_close and close and high and low):
                continue
            median = statistics.median(d[7] or 0.0 for d in days[i - LOOKBACK : i])
            if median < MIN_TURNOVER_LACS or prev_close < 10 or high == low:
                continue
            overnight, intraday = open_ / prev_close - 1, close / open_ - 1
            if abs(overnight) >= 0.195:
                continue
            weight = median  # traded value as a stand-in for index weight
            s = day_sums[day]
            s[0] += 1
            s[1] += overnight
            s[2] += intraday
            s[3] += close / prev_close - 1
            s[4] += weight
            s[5] += weight * overnight
            s[6] += weight * intraday
            s[7] += weight * (close / prev_close - 1)

    for name in INDEXES:
        levels = index[name]
        sessions = sorted(levels)
        rows = []  # per day: stocks ew (on, id, cc), stocks tw (on, id, cc), index (on, id, cc)
        for previous, day in pairwise(sessions):
            s = day_sums.get(day)
            if not s or s[0] < 100:
                continue
            open_, close = levels[day]
            prior_close = levels[previous][1]
            rows.append(
                (
                    s[1] / s[0], s[2] / s[0], s[3] / s[0],
                    s[5] / s[4], s[6] / s[4], s[7] / s[4],
                    open_ / prior_close - 1, close / open_ - 1, close / prior_close - 1,
                )
            )  # fmt: skip
        cols = list(zip(*rows, strict=True))
        mean = [statistics.fmean(c) for c in cols]
        print(f"\n{name}: {len(rows)} sessions, mean daily return")
        print(f"{'':28} {'overnight':>10} {'intraday':>10} {'close-close':>12}")
        for label, first in (
            ("stocks, equal-weighted", 0),
            ("stocks, turnover-weighted", 3),
            ("index (as published)", 6),
        ):
            overnight, intraday, both = mean[first : first + 3]
            print(f"{label:28} {overnight:>+10.3%} {intraday:>+10.3%} {both:>+12.3%}")
        b_on, r_on = slope(list(cols[3]), list(cols[6]))
        b_id, r_id = slope(list(cols[4]), list(cols[7]))
        b_cc, r_cc = slope(list(cols[5]), list(cols[8]))
        print("index move per unit of its stocks' (turnover-weighted) move, across days:")
        print(f"  overnight   slope {b_on:.2f}  correlation {r_on:.2f}")
        print(f"  intraday    slope {b_id:.2f}  correlation {r_id:.2f}")
        print(f"  close-close slope {b_cc:.2f}  correlation {r_cc:.2f}")
        spread = statistics.pstdev(cols[6]) / statistics.pstdev(cols[3])
        print(f"  spread of the index's overnight move / of its stocks': {spread:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
