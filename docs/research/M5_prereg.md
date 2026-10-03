# M5 pre-registration: is the reaction to a disclosure tradeable intraday?

- **Study:** `m5-intraday-reaction` · **Registered:** 2026-10-03, before any
  one-minute bar was fetched or any intraday return computed.
- **Config:** `configs/studies/m5_reaction.yaml`, SHA-256 (LF line endings)
  `1d064d8618cf4229bda5eb932160ab76c29a84b6fcf029cd42d1f633dd36fb5a`.
  The engine refuses to run if the file's hash differs. Any change is a new
  study and a new trial.
- **Taxonomy:** `taxonomy-v1+6f16e1c9`. **Costs:**
  `configs/costs/india_equity.yaml` (verified rates, T6.1).
- **Disclosure:** at registration no one-minute bar exists in the project
  (the broker token is not set up) and the M3 daily study has not been run,
  so no return of any kind has been seen for these events.

## 1. Question and gate

After a disclosure, is there a positive abnormal return left for a trader
who enters **after our measured feed latency** and pays **verified intraday
costs**? This is gate G1b (DESIGN, Gates): the intraday strategy passes
only if its out-of-sample net expectancy has a confidence bound above zero.
"No tradeable remainder" is an acceptable, reportable answer.

## 2. Data

- NSE filings, typed by the taxonomy, linked to securities, the same events
  as M3 (same availability rule: a filing without a dissemination second
  counts as available at the end of its minute).
- One-minute bars from the broker (Upstox), from 2022-01-01, for each
  event's previous, event and next session, and for the Nifty 500.
- A run needs bars for at least 95% of the in-scope events since 2022-01
  (`gats bars coverage`) and is refused below that.

## 3. Events and scope

Confirmatory types are M3's: ORDER_WIN, RATING_UP, BUYBACK, BONUS_SPLIT,
PRESS_RELEASE. **The run's scope** is the subset in scope after gate G1
(the types that passed, or those the human keeps if none passed). The scope
is written to the run log before any bar-based return is computed, and the
multiple-testing family is exactly scope × confirmatory exits.

Limitation, stated in advance: scope is chosen on M3's test period, which
overlaps this study's. For filings made during the session the windows do
not overlap (M3 enters at the next session's open; this study exits by the
event session's close), but the selection is not independent. The report
therefore also shows the results for filings dated after the M3 run.

## 4. Hypotheses

For each type in scope and each confirmatory exit: H1, the mean **net
abnormal return** from entry to exit is > 0 (one-sided). Long only.

## 5. Trades

- **Decision time** = availability + feed latency + 20 s (extraction) + 5 s
  (order). **Feed latency is measured**: the 95th percentile of (first seen
  by our recorder − exchange dissemination) over live filings of the 30
  days before the run. With fewer than 500 live filings the run is refused.
- **Entry:** the open of the first one-minute bar that starts at or after
  the decision time, if the decision is in a regular session before 15:00
  IST; otherwise the first bar of the next regular session. The bar must
  start within 5 minutes of the earliest tradeable minute, else the event
  is dropped (`no_bar`).
- **Exits (confirmatory):** the open of the first bar starting 5, 15, 30
  and 60 minutes after the entry bar, and the square-off at 15:20 IST. An
  exit that would fall after 15:20 does not exist for that event.
- **Exits (reported only):** 1 minute, and the next session's close (a
  delivery trade, with delivery charges).
- **Abnormal return** = stock return − Nifty 500 return over the same
  minutes. **Net** = abnormal − costs.
- **Costs:** the verified cost model's charges for a Rs 50,000 intraday
  round trip at the trade's own prices (latest regime), plus 5 basis points
  of slippage per side. The slippage figure is an assumption; results at 0
  and 10 basis points are reported beside it, and only the 5 bp figure
  decides the gate.

## 6. Filters (applied before returns are looked at)

Series EQ; 20-session median traded value before entry ≥ Rs 1 crore;
previous close ≥ Rs 10; a second event of the same type for the same
security within 5 sessions is dropped; an entry bar that is flat at the
day's high is treated as locked at the circuit and the event dropped.
Every filter's count is reported.

## 7. Split and holdout

- **Train:** entries 2022-01-01 → 2023-12-31. **Test (holdout):** entries
  2024-01-01 → 2026-09-30.
- G1b is decided on the test period only, computed once. Nothing is tuned
  after test results are seen.

## 8. Statistics

As in M3: per type × exit, N, mean and median net abnormal return, hit
rate, a t-statistic with standard errors clustered by entry date, a
date-clustered bootstrap 95% CI (10,000 resamples, seed 20261003),
one-sided p-values, Benjamini–Hochberg at q = 0.05 over the family (types
in scope × 5 confirmatory exits), and FCR-adjusted lower bounds for the
selected tests.

## 9. Pass criteria (G1b)

A (type, exit) passes only if, in the test period: N ≥ 100 after filters;
mean net abnormal return > 0; selected by BH at q = 0.05; FCR-adjusted
bootstrap lower bound > 0. If nothing passes, the intraday strategy stops
here and the decision goes to the human.

## 10. Reporting

`reports/M5_reaction_curves.md`, regenerated by one command from the saved
per-event frame: the decision table, train and test for every type and
exit (gross, abnormal, net), the session and overnight strata, the 0 and
10 bp slippage sensitivities, results at the median latency, filings
after the M3 run date, the measured latency and the costs used, and every
filter's count. The run is recorded in the experiment registry as a
holdout run.
