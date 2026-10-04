# Research trial log

Since T6.6 every research run is recorded in the `experiments` table, before
it starts (`gats experiments list`); the number of distinct designs there
feeds the multiple-testing corrections (BH-FDR, deflated Sharpe). No real
run was logged in this file before the registry existed, so there was
nothing to import. The table below stays for hand-written notes on a trial.

| # | Date | Study / pre-registration | Hypothesis | Data window (train / test) | Result | Notes |
|---|---|---|---|---|---|---|
| 20261004T061547Z | 2026-10-04 | M3_prereg.md (m3-disclosure-drift) | 20 confirmatory tests: ORDER_WIN, RATING_UP, BUYBACK, BONUS_SPLIT, PRESS_RELEASE x d0, d1, d3, d5 | train ≤ 2023-12-31 / test ≥ 2024-01-01 | no edge found | reports/M3_event_study.md |
