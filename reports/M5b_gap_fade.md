# M5 arm B (exploratory): the gap fade

Study `m5b-gap-fade`, config `da58f09d0308` (registered in `docs/research/M5_prereg.md`, Amendment 1), run 20261004T092946Z, experiment #3.

**Exploratory result: not supported: the filing adds nothing beyond its matched controls.**

Suggested by M3's own data, so this history cannot confirm it whatever it shows. The trade: short at the open, cover at the close, for ORDER_WIN filings made outside market hours that opened at least 1% up. A control is a gap-up of the same size bin, on the same date, in the same liquidity bucket, of a company that filed nothing.

Lower bounds are one-sided, 95%, from a bootstrap over dates.

## Test period

| Measure | N | Mean | Lower bound | t |
|---|---|---|---|---|
| Short, before costs (all events) | 1063 | +0.75% | +0.54% | 6.06 |
| Short, after costs (all events) | 1063 | +0.52% | +0.31% | 4.21 |
| Short, after costs (matched events) | 885 | +0.46% | +0.23% | 3.45 |
| Matched controls, before costs | 885 | +0.79% | +0.61% | 7.08 |
| Filing effect (event minus its controls) | 885 | -0.10% | -0.31% | -0.82 |
| Filing effect, filed the same morning only | 98 | +0.16% | -0.43% | 0.45 |

1063 events, 885 with at least one control (20.5 controls each on average).

## Train period

| Measure | N | Mean | Lower bound | t |
|---|---|---|---|---|
| Short, before costs (all events) | 382 | +0.73% | +0.47% | 4.58 |
| Short, after costs (all events) | 382 | +0.50% | +0.24% | 3.15 |
| Short, after costs (matched events) | 320 | +0.35% | +0.06% | 2.00 |
| Matched controls, before costs | 320 | +0.86% | +0.62% | 5.91 |
| Filing effect (event minus its controls) | 320 | -0.28% | -0.59% | -1.55 |
| Filing effect, filed the same morning only | 46 | -0.78% | -1.47% | -1.72 |

382 events, 320 with at least one control (19.2 controls each on average).

## Every filing accounted for

| Outcome | Filings |
|---|---|
| kept | 1445 |
| in_session | 2758 |
| too_late | 84 |
| outside_window | 7 |
| no_symbol | 1 |
| no_price | 324 |
| ex_date | 15 |
| low_price | 35 |
| illiquid | 431 |
| locked | 13 |
| no_gap_up | 987 |
| circuit_gap | 2 |
| near_band | 16 |
| duplicate | 136 |
