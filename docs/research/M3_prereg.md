# M3 pre-registration: do disclosures predict tradeable abnormal returns?

- **Study:** `m3-disclosure-drift` · **Registered:** 2026-10-03, before any
  event return was computed.
- **Config:** `configs/studies/m3_event_study.yaml`, SHA-256 (LF line
  endings) `3b3ec2ca2c67a3f0eba704d334bdb75193fed1133e26c835027b4234c9786835`.
  The engine refuses to run if the file's hash differs. Any change is a new
  study and a new trial.
- **Taxonomy:** `taxonomy-v1+6f16e1c9` (`configs/event_taxonomy.yaml`).
- **Disclosure:** before registration, the only returns computed on this
  data were split/bonus ex-date checks for T2.8 (10 cases in Sep 2025, 4 in
  Sep 2026). They are unrelated to these hypotheses, and no announcement
  type was looked at against prices.

## 1. Question and gate

For each disclosure type, is the abnormal return from the **first session
open after the filing became available** positive on average, **net of
costs**, in a **held-out test period**? This is gate G1 (DESIGN §Gates). "No
edge found" is an acceptable, reportable answer.

## 2. Data

- Filings: NSE corporate announcements, 2019-10-01 → 2026-09-30, from
  `gats backfill announcements --source nse` (raw payloads in the raw
  store). BSE filings are **not** used. M3 prices on NSE, and NSE-only
  availability times can only make entries later, never earlier.
- Prices: NSE `sec_bhavdata_full` (`OPEN_PRICE`, `CLOSE_PRICE`,
  `PREV_CLOSE`, `TURNOVER_LACS`), series EQ, with split/bonus multipliers
  from NSE corporate actions (T2.8: `PREV_CLOSE` is not adjusted).
- Benchmark: Nifty 500 index closes and opens (`ind_close_all`).
- Each filing is linked to its NSE listing through the security master
  (T2.3/T2.4). The filing's symbol is translated to its symbol on the
  trading dates (T2.2).

## 3. Events

- Event type = the filing's type under the pinned taxonomy. NSE filings are
  typed alone (no BSE twin).
- Buyback progress reports (daily reports, closure, post-buyback
  announcements) are excluded; the announcement is the event.
- **Availability:** `available_at` (backfill = NSE dissemination time).
  Filings from before NSE published seconds (no `exchdisstime`, `an_dt` at
  `:00`, before ~Aug 2020) count as available 60 s later, at the end of
  their minute.
- **Duplicates:** a second filing of the same type for the same security
  within 5 sessions of the first is dropped.

## 4. Hypotheses

**Confirmatory (eligible to pass G1).** Each predicts a positive mean net
abnormal return:

| Type | Why the other side might be slow |
|---|---|
| ORDER_WIN | Order size relative to the company is not obvious from the headline; small caps get little coverage. |
| RATING_UP | Rating upgrades lower funding costs; equity investors in small names react slowly to credit news. |
| BUYBACK | A buyback announcement signals undervaluation and creates demand. |
| BONUS_SPLIT | Retail interest and liquidity after bonus/split announcements. |
| PRESS_RELEASE | Companies publicise good news; small-cap press releases get little coverage. |

**Exploratory (reported, never pass):** RESULTS, DIVIDEND, ACQUISITION,
FUNDRAISE, PLEDGE_CHANGE, BUSINESS_UPDATE, MGMT_CHANGE, RATING_DOWN,
PENALTY_LITIGATION, INSOLVENCY, HOLDING_CHANGE, BOARD_OUTCOME. Their sign is
unknown from metadata, or a positive edge would require shorting, which is
not available overnight in Indian cash equities. Results by liquidity bucket
are also exploratory.

## 5. Trades

- **Entry:** `OPEN_PRICE` of the first regular session whose open (09:15 IST)
  is strictly after `available_at`. Special sessions (Muhurat, weekend
  budget days) are never entry sessions.
- **Exits** (each a separate test): close of the entry session (`d0`) and
  of the 1st, 3rd and 5th regular sessions after it (`d1`, `d3`, `d5`).
- **Stock return:** `CLOSE_exit × Π m / OPEN_entry − 1`, where `m` are the
  split/bonus share multipliers of ex-dates after the entry open, up to and
  including the exit session.
- **Abnormal return:** stock return − Nifty 500 return over the same
  open→close window (market-adjusted; no beta estimation).
- **Net:** abnormal return − 0.50%. This flat round trip is an **assumption**
  covering brokerage, taxes, exchange fees and slippage until M6 builds the
  verified cost model. It is deliberately conservative. Gross results are
  reported as well.

## 6. Filters (applied before returns are looked at)

- Series EQ (trade-for-trade BE/BZ excluded).
- Median traded value over the 20 sessions before the event ≥ ₹1 crore, and
  previous close ≥ ₹10.
- Untradeable entries are skipped: the entry session is locked (HIGH = LOW),
  or `|OPEN / PREV_CLOSE − 1| ≥ 19.5%` (at or near a circuit limit).
  Historical price bands are not available, so this proxy replaces "near the
  band".
- Events whose window contains a corporate action flagged `needs_review`
  (rights, demerger, bonus preference shares) are excluded.
- ASM/GSM history exists only from 2026-10-02, so it cannot be applied
  historically. Trade-for-trade is covered by the EQ filter.

## 7. Split and holdout

- **Train:** entries 2019-10-01 → 2023-12-31. **Test (holdout):** entries
  2024-01-01 → 2026-09-30.
- The train period is reported for context. **G1 is decided on the test
  period only, computed once.** Nothing is tuned after test results are
  seen; a new idea needs a new pre-registration and counts as a new trial.

## 8. Statistics

- Per type × exit: N, mean and median net abnormal return, hit rate (share
  > 0), and a t-statistic with standard errors clustered by entry date.
- 95% CI by date-clustered bootstrap (10,000 resamples, seed 20261003).
- One-sided p-values (H1: mean > 0) from the clustered t-statistic.
- **Multiple testing:** Benjamini–Hochberg at q = 0.05 over the
  confirmatory family (5 types × 4 exits = 20 tests). For the R tests BH
  selects, CIs are FCR-adjusted (Benjamini–Yekutieli): one-sided lower
  bounds at level 1 − R·q/20.

## 9. Pass criteria (G1)

A confirmatory (type, exit) passes only if, **in the test period**, all of
the following hold:

1. N_test ≥ 100 events after filters;
2. mean net abnormal return > 0;
3. it is selected by BH at q = 0.05;
4. its FCR-adjusted bootstrap lower bound is > 0.

If nothing passes, record "no edge found at daily horizon" in
`docs/research/M3_decision.md`, propose next hypotheses (DESIGN §Backlog) and
stop for the human. If some pass, they define M4's scope.

## 10. Reporting

- `reports/M3_event_study.md`, regenerated by one command: every type and
  exit for train and test, gross and net.
- Also reported: event counts lost to each filter, CAR plots, and three
  events spot-checked by hand against the raw bhavcopy.
- The run is logged in `docs/research/trials.md`: 20 confirmatory tests,
  plus exploratory ones marked as such.
