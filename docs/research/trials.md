# Research trial log

Since T6.6 every research run is recorded in the `experiments` table, before
it starts (`gats experiments list`); the number of distinct designs there
feeds the multiple-testing corrections (BH-FDR, deflated Sharpe). No real
run was logged in this file before the registry existed, so there was
nothing to import. The table below stays for hand-written notes on a trial.

| # | Date | Study / pre-registration | Hypothesis | Data window (train / test) | Result | Notes |
|---|---|---|---|---|---|---|
| 20261004T061547Z | 2026-10-04 | M3_prereg.md (m3-disclosure-drift) | 20 confirmatory tests: ORDER_WIN, RATING_UP, BUYBACK, BONUS_SPLIT, PRESS_RELEASE x d0, d1, d3, d5 | train ≤ 2023-12-31 / test ≥ 2024-01-01 | no edge found | reports/M3_event_study.md |
| (not run yet) | 2026-10-04 | M5_prereg.md, Amendments 1 and 2, arm A (m5-intraday-reaction, config `92f74c45790b`) | confirmatory, 5 tests: ORDER_WIN filings made during market hours, entry after the measured p95 latency, exits m5, m15, m30, m60, close | train 2022-01-01 to 2023-12-31 / test ≥ 2024-01-01 | registered, waiting for one-minute bars and measured latency | replaces the unrun configs `1d064d8618cf` (5 types, both strata) and `71bdb113ada8` (no exploratory types) |
| 20261004T092219Z | 2026-10-04 | M5_prereg.md, Amendment 1, arm B (m5b-gap-fade, config `da58f09d0308`) | EXPLORATORY, suggested by M3's data: short the open after an out-of-hours ORDER_WIN filing that gapped the stock up ≥ 1%, cover at the close; counts only beyond matched non-filing gap-ups | train ≤ 2023-12-31 / test ≥ 2024-01-01 (M3's test period, read again) | **not supported**: the filing adds nothing beyond matched gap-ups (test: filing effect -0.10%, lower bound -0.31%, 885 events; the short +0.46% after costs, its controls +0.79% before costs) | reports/M5b_gap_fade.md; experiment #2, reproduced as #3 (20261004T092946Z) to save the frames; the idea stops here |
| (not run yet) | 2026-10-04 | M5_prereg.md, Amendment 2 (m5-intraday-reaction, config `92f74c45790b`) | EXPLORATORY, 85 looks: the taxonomy's 17 other event types x 5 exits through arm A's pipeline; N, mean and 95% interval only, no test, never part of G1b | train 2022-01-01 to 2023-12-31 / test ≥ 2024-01-01 | registered, waiting for one-minute bars and measured latency | a type is promoted only by a new pre-registration, tested on forward or paper data |
