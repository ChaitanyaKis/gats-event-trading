# M3 decision: gate G1

**Result: G1 is not passed. No edge found at the daily horizon.**

Run `20261004T061547Z` (2026-10-04), config `3b3ec2ca2c67…` as pre-registered
in [`M3_prereg.md`](M3_prereg.md), taxonomy `taxonomy-v1+6f16e1c9`. Full
tables: [`reports/M3_event_study.md`](../../reports/M3_event_study.md). The
holdout (entries from 2024-01-01 to 2026-09-30) was read once, by this run.

The decision at this gate is the human's. This note records what was found,
what it does and does not show, and the options.

## What was tested

Buy at the first open after a filing became public, hold to the close of
that day or of 1, 3 or 5 sessions later, measured against the Nifty 500,
minus a flat 0.50% round trip. Five filing types were hypotheses (order
wins, rating upgrades, buybacks, bonus and split announcements, press
releases), over seven years of NSE filings: 1,042,483 filings, all typed,
99.98% linked to a company, 1,738 sessions of prices.

## What was found (test period, after costs)

| Type | Events | Same day | +1 | +3 | +5 sessions |
|---|---|---|---|---|---|
| Order win | 2,447 | −0.97% | −1.05% | −1.08% | −1.21% |
| Rating upgrade | 91 | −0.31% | −0.78% | −0.40% | −0.35% |
| Buyback | 195 | −0.48% | −0.39% | −0.04% | −0.36% |
| Bonus or split | 327 | −0.79% | −1.02% | −1.49% | −1.84% |
| Press release | 8,662 | −0.81% | −0.91% | −0.98% | −1.08% |

All twenty tests have a mean below zero. None is selected by the
Benjamini–Hochberg correction, so no confidence bound is above zero.
Rating upgrades also miss the minimum of 100 events. The train period
(2019 to 2023) shows the same picture, so this is not one unlucky period.

**It is not only the costs.** Before costs, order wins still lose to the
market: −0.47% the same day and −0.71% after five sessions. The closest any
hypothesis comes to zero is buybacks at +3 sessions (−0.04% after costs,
interval −0.72% to +0.68%): indistinguishable from nothing.

## Is the yardstick tilted? (a check, not a test)

Every filing type came out negative before costs, including neutral ones
such as management changes. That raises a question about the measurement
before it says anything about filings. `scripts/m3_placebo.py` applies the
same universe filters and prices to every ordinary stock-day, with no
filing involved:

| Period | Stock-days | Same day | +1 | +3 | +5 |
|---|---|---|---|---|---|
| Test | 967,227 | −0.12% | −0.10% | −0.05% | −0.03% |
| Train | 947,044 | −0.15% | −0.10% | −0.01% | +0.06% |

So there is a small tilt: an average liquid stock bought at the open lags
the index by about 0.1% that day, gone within a week. It explains a part of
the result and not the result. Order wins are about 0.35% worse than an
ordinary day at once and about 0.7% worse after five sessions.

**Reading:** by the next open the news is already in the price, and more
than in it. The stock opens up on the filing and gives some of it back.
Buying the open is buying from the people who read the filing first.

## A breakdown by when the filing appeared (exploratory)

Not pre-registered, so it can suggest a hypothesis and prove nothing. Order
wins, test period, before costs:

| Filed | Events | Same day | +1 | +5 |
|---|---|---|---|---|
| During the session | 1,011 | −0.40% | −0.49% | −0.55% |
| After the close | 1,070 | −0.54% | −0.76% | −1.01% |
| Before the open | 175 | −0.33% | −0.04% | +0.32% |
| On a weekend | 191 | −0.55% | −0.15% | −0.79% |

About four order wins in ten are filed while the market is open. For those,
this study's entry (the *next* day's open) comes after a whole afternoon of
reaction. What happens in the first minutes after such a filing is exactly
what this study cannot see.

## What this does and does not show

- **Shows:** there is no slow drift to collect. A strategy that reads a
  filing overnight and buys the next morning loses money, on every type
  tested, in and out of sample. That closes the daily version of S1.
- **Does not show:** anything about acting within minutes of a filing made
  during market hours (the strategy as designed in DESIGN, and the question
  M5's pre-registered reaction study asks), or about order size relative to
  the company (T4.7, pre-registered separately, same next-open entry).
- **Limits:** older filings are typed less completely (the taxonomy was
  written on 2026 categories: 40 to 68% of non-noise filings are untyped in
  2019 to 2023, 34% in 2024, 22% in 2025, 17% in 2026). Untyped filings
  cannot bias the typed results; they do cost events. Backfilled filings
  carry the exchange's timestamp as their availability, the most favourable
  assumption possible for the strategy, and it still loses.

## Options (the human decides)

1. **Test the intraday hypothesis (M5, gate G1b).** Already pre-registered
   and coded. Needs one-minute bars (a free Upstox account for the
   read-only data token) and a few weeks of the recorder running, because
   the study enters after this system's *measured* delay, not an assumed
   one. M3 makes this both more interesting and harder: the reaction is
   fast, so any edge lives in the first minutes, and in today's first
   measurements (only two NSE filings) the recorder saw a filing about a
   minute after the exchange published it.
2. **Run the pre-registered order-size study (T4.7).** Needs the 300 labels
   and the revenue backfill. It keeps M3's next-open entry and only filters
   by size, so M3's result is a poor omen for it. It would be the second use
   of this test period (already accounted for in its registration).
3. **Pivot.** The backlog has S2 (overnight global cues) and S3 (a
   mean-reversion baseline). The data also suggests a new one: the fade
   after the open. That would be a different strategy (short side, intraday,
   its own costs and rules), needing its own pre-registration and, to be
   honest, fresh data: this test period has now been looked at.
4. **Stop the trading research here.** The recorder, the point-in-time data
   and this result are finished pieces of work in their own right. "No edge
   found" was always one of the two possible answers.

**Claude's recommendation: option 1, then decide.** The strategy was always
meant to act within minutes; the daily study was the cheap screen, and what
it found (the news is priced by the next open) is what the intraday thesis
predicts, not what refutes it. It is also the cheapest next step for you:
one account and one token. Labelling 300 filings (option 2) is worth your
hours only if G1b passes, since the intraday strategy uses the same order
sizes. What should not happen is quietly changing this study's rules until
something passes: that is how a backtest lies.
