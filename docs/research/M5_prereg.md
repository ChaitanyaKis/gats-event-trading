# M5 pre-registration: is the reaction to a disclosure tradeable intraday?

- **Study:** `m5-intraday-reaction` · **Registered:** 2026-10-03, before any
  one-minute bar was fetched or any intraday return computed. **Amended:**
  2026-10-04 (Amendments 1 and 2, at the end), still before any bar or
  intraday return. Where they differ from sections 1 to 10, the amendments
  rule; where they differ from each other, the later one does.
- **Config:** `configs/studies/m5_reaction.yaml`, SHA-256 (LF line endings)
  `92f74c45790b7176230343c43320ac530997e7ae0c148e7d25b669afe65f3766`.
  The engine refuses to run if the file's hash differs. Any change is a new
  study and a new trial.
- **Taxonomy:** `taxonomy-v1+6f16e1c9`. **Costs:**
  `configs/costs/india_equity.yaml` (verified rates, T6.1).
- **Disclosure:** at registration no one-minute bar exists in the project
  (the broker token is not set up) and the M3 daily study has not been run,
  so no return of any kind has been seen for these events. (At the
  amendment M3 had been run and read; see Amendment 1.)

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

## Amendment 1 (2026-10-04): after gate G1, before any intraday return

Gate G1 was not passed (`docs/research/M3_decision.md`). The human's
decision: run M5, with the two arms below. This amendment was committed
before any one-minute bar was fetched and before anything in it was
computed. The config hash at the top is the amended one; it replaces the
original (`1d064d86…fb5a`, never run, in this file's git history).

**What had been seen when this was written:** M3's report, including its
exploratory split of order wins by the time of filing; M3's placebo on
ordinary stock-days; the measurement check in A1.3. **Not seen:** any
one-minute bar, any intraday return, and none of arm B's numbers (gap-up
days after a filing, or their controls).

### A1.1 Arm A (primary, confirmatory): the original thesis

Act within minutes of a filing made while the market is open.

- **Scope: ORDER_WIN.** This is the scope decision of section 3. M3's other
  four types are out of the study. The family is 1 type × 5 confirmatory
  exits = 5 tests. The scope can still be changed by a dated amendment
  until the first one-minute bar is fetched, and not after.
- **Only filings made during market hours can pass the gate:** those whose
  decision time (availability + the measured delay) falls inside a regular
  session before the 15:00 cutoff, so the entry is in the same session,
  minutes after the filing (config: `entry.confirmatory_strata: [session]`).
  The statistics of section 8 and the pass criteria of section 9 are
  computed on these events only; N ≥ 100 counts these events only.
- Filings entered at the next session's open (made after the cutoff, after
  hours, before the open, on weekends and holidays) are still computed and
  reported, and cannot pass. M3 measured that entry at the daily horizon
  and it lost.
- **Unchanged:** entry after the measured live feed latency (95th
  percentile) plus 25 s, the exits, the costs, the filters, the split, the
  statistics.
- **New, reported only:** for every session event, the same exits on the
  same stock at the same clock time one session earlier (no filing), before
  costs. It is not a pass criterion and cannot rescue a failing result.

### A1.2 Arm B (exploratory): the gap fade

**Exploratory, and suggested by M3's own data** (order wins lost about
0.5% against the index from the entry day's open to its close). Whatever
this history shows, it cannot confirm the idea. If arm B is supported, the
only confirmation is forward: filings recorded after 2026-10-04, or paper
trading, under a new pre-registration. A rerun on the M3/M5 history, with
any change, never counts.

- **Config:** `configs/studies/m5b_gap_fade.yaml`, SHA-256 (LF line endings)
  `da58f09d0308247d817c159e5eb160bd7f2450446a42225bab425f5f6e5b0e9b`.
  Study `m5b-gap-fade`; `gats research gap-fade` refuses any other file.
  Daily prices only; the report is `reports/M5b_gap_fade.md`.
- **Events:** ORDER_WIN filings on NSE (same taxonomy, linking and
  availability rule as M3), 2019-10-01 → 2026-09-30, made **outside market
  hours**: not inside a regular session, and public by 09:00 IST of the
  entry day, which is the next regular session to open. NSE's opening
  auction takes orders from 09:00 to 09:08; a later filing cannot be traded
  at the open and is dropped (`too_late`). One event per company and day.
- **Trade:** sell short at the session's official open, buy back at its
  official close (bhavcopy OPEN_PRICE, CLOSE_PRICE). Gross = 1 − close/open.
  Net = gross − the verified cost model's intraday charges for a Rs 50,000
  sell and buy at those prices − 5 basis points of slippage per side.
- **A stock-day is tradeable only if:** series EQ; not the ex-date of a
  corporate action; previous close ≥ Rs 10; median traded value of the 20
  sessions before it ≥ Rs 1 crore; high ≠ low; it gapped up, 1% ≤ open /
  previous close − 1 < 19.5%; and the gap is at least 1 percentage point
  below the stock's upper price band (no entry within 1% of the band;
  stocks with no band, or with none on record, are exempt).
- **Matched control:** for each event, every other tradeable gap-up (same
  rules) on the **same date**, in the **same liquidity bucket** (median
  traded value Rs 1–10 crore, 10–100 crore, 100 crore and more) and the
  **same gap bin** (1–2%, 2–3%, 3–5%, 5–10%, 10–19.5%), whose company made
  no filing of any type, in our records, from the previous close to that
  day's close. The event's control return is the mean gross short return
  of these stock-days.
- **Filing effect** = the event's gross short return − its control return.
  Events with no control in their cell are reported and have no effect.
- **Split:** as M3: train to 2023-12-31, test from 2024-01-01. The test
  period is M3's, read again, which is why this arm is exploratory.
- **"Supported"** means, in the test period, all three: at least 100 events
  with a control; their mean net short return > 0 with a one-sided 95%
  lower bound > 0; their mean filing effect > 0 with a one-sided 95% lower
  bound > 0 (bootstrap over dates, 10,000 resamples, seed 20261004). The
  filing effect counts only if it beats the control: a fade no larger than
  the controls' is the ordinary gap fade and says nothing about filings.
- **If it is not supported** the idea stops here. Variations on this
  history are new trials and need fresh data.
- **Limits, stated in advance.** Price bands were recorded only from
  2026-10, so earlier days use each stock's latest recorded band as a
  stand-in. Daily data cannot see whether the broker allowed an intraday
  short in that stock that day, whether the stock was locked at its upper
  circuit when the short had to be covered (such a short goes to auction at
  a penalty; here it is valued at the close), or the real fill at the open
  and at the square-off, which happens some minutes before the close.
  Controls match on date, liquidity and gap size, not on sector or on why
  the stock gapped.

### A1.3 The measurement check behind both arms

M3's placebo found that an ordinary liquid stock loses about 0.1% to the
Nifty 500 between the open and the close. Before any intraday magnitude is
trusted: is that real, or do the stock's open and the index's open measure
different things (an index "open" built partly from previous closes would
book part of the overnight move as intraday)? `scripts/open_measure_check.py`,
1,714 sessions, the placebo's universe, mean daily return:

| | Previous close → open | Open → close | Close → close |
|---|---|---|---|
| Stocks, equal-weighted | +0.315% | −0.220% | +0.089% |
| Stocks, weighted by traded value | +0.203% | −0.134% | +0.067% |
| Nifty 500, as published | +0.140% | −0.086% | +0.054% |
| Nifty 50, as published | +0.101% | −0.057% | +0.044% |

Across days the Nifty 500's overnight move follows its stocks' overnight
move with slope 0.87 and correlation 0.99; the close-to-close slope, which
cannot be mismeasured, is 0.80 (correlation 0.96). A stale index open would
show an overnight slope well below the close-to-close one. It does not.

**Reading: no sign of a mismatch; the tilt is a size effect.** Every row
gains overnight and gives some back in the day, and the smaller the stocks
the stronger the pattern (Nifty 50 < Nifty 500 < traded-value-weighted <
equal-weighted). So an ordinary small stock bought at the open really does
lag the index by the close. Consequences, both already in the design:

- Arm A's gate is unchanged and conservative for a long (the tilt works
  against it); the previous-session baseline shows how much of a result is
  the tilt.
- Arm B's short gains from the same tilt whether or not there is a filing,
  which is why it needs the matched control. Its returns use no index.

### A1.4 Trials

Both arms are logged in `docs/research/trials.md` and, when run, in the
experiment registry: arm A as the study's one confirmatory design, arm B as
one exploratory design that reads a holdout again.

## Amendment 2 (2026-10-04): every other event type, as exploratory

The human's decision on arm A's scope, committed while no one-minute bar
existed and no intraday return had been computed (arm B, already run, used
daily prices only). The config hash at the top is the amended one; it
replaces Amendment 1's (`71bdb113…5260`, never run).

- **Confirmatory, unchanged:** ORDER_WIN filings made during market hours,
  5 exits, 5 tests. Nothing below joins this family, so its correction is
  the one already registered.
- **Exploratory: the taxonomy's 17 other event types.** ACQUISITION,
  BOARD_OUTCOME, BONUS_SPLIT, BUSINESS_UPDATE, BUYBACK, DIVIDEND, FUNDRAISE,
  HOLDING_CHANGE, INSOLVENCY, MGMT_CHANGE, PENALTY_LITIGATION,
  PLEDGE_CHANGE, PRESS_RELEASE, RATING_DOWN, RATING_OTHER, RATING_UP,
  RESULTS. Amendment 1 had put M3's other four types out of the study;
  they are back, as exploratory only.
- **Same pipeline as arm A:** the same kind of filing (made during market
  hours), entry after the measured delay, exits, costs and filters. For
  each type and confirmatory exit the report gives N, the mean net abnormal
  return and a 95% interval (bootstrap over dates), with the mean before
  costs and the train period beside them. No p-value and no pass: they are
  not tests and cannot decide G1b.
- **Not run:** AGM_NOISE (procedural filings) and OTHER (untyped) are not
  event types. LISTING is typed from BSE filings only, and this study reads
  NSE's.
- **Entries are long, as in arm A.** For bad-news types (a downgrade, a
  penalty, insolvency, a pledge) the mean before costs shows the direction
  of the reaction; trading it short would be a design of its own.
- **85 looks** (17 types × 5 exits), so some will look good by chance.
  **An exploratory type can become a confirmatory test only in a new
  pre-registration, tested on forward or paper data**, never on this
  history.
- **Order size is not in arm A.** No amount-against-revenue filter or
  feature enters this study. That needs M4's labelled extraction and has
  its own pre-registration (`M4_prereg.md`, T4.7).
- **Bars.** The run still needs bars for 95% of the confirmatory events. An
  exploratory type is reported only if bars cover 95% of its own events;
  otherwise the report names it and the share covered. G1b is decided by
  the first run. A later run of this design is a reproduction (it may add
  exploratory types whose bars have arrived since) and cannot change the
  decision.
- **Also reported:** how many filings were left out before any price was
  read (not linked to a company, an excluded category, before minute data
  exists, no single ISIN).
- Logged in `docs/research/trials.md`.
