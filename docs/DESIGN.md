# GATS Design (condensed)

This is the source of truth for *why*. The ROADMAP says *what* and *when*.

## Thesis

A solo operator can't win speed races against colocated trading firms, and
frequency multiplies expectancy whatever its sign. The only realistic edge is
**attention breadth plus interpretation at human speed, where large players
don't bother**:

- read 100% of exchange filings within seconds,
- including under-covered small- and mid-caps,
- judge each event's *magnitude relative to the company*, and
- trade the drift that follows,
- only if it survives costs, latency and honest out-of-sample tests.

The system's product is **validated edge**, not trades. No trade happens
unless an edge has passed every gate. "No edge found" is a valid outcome.

## Constraints (verify current values before relying on them)

| Constraint | Detail |
|---|---|
| Market | NSE/BSE cash equities, 09:15–15:30 IST. The recorder runs 24/7; trading runs in market hours. |
| Crypto | **Excluded.** Indian VDA tax is 30% on gains with no loss set-off and 1% TDS, which breaks active strategies. |
| Speed | SEBI retail algo framework (from April 2026): ≤ 10 orders/sec without strategy registration, static IP required for API trading, market orders need market protection. |
| Costs | Brokerage, STT, exchange fees, SEBI fee, stamp duty, GST (and DP charges for delivery). A rough early estimate is ~0.1% round trip for intraday; M6 builds the verified model. |
| Tax | Intraday equity is speculative business income. Delivery gains are STCG/LTCG. Informational only; this system does not give tax advice. |
| Evidence | SEBI's FY23 study: 71% of intraday traders lost money, and 80% of those making > 500 trades/yr. Frequency without edge is a loss accelerator. |

## Strategy backlog

- **S1 (primary): disclosure reaction in under-covered NSE/BSE stocks.**
  - Events: order wins, results, rating changes, buybacks, promoter buying,
    pledge changes, fund raising, M&A.
  - Magnitude matters: an order worth 40% of revenue matters; one worth 0.4%
    doesn't.
  - Horizon: minutes to 3 days, event-driven.
  - Fails if: price is at the circuit limit, liquidity is thin, the stock is
    on a surveillance list, or the drift is smaller than costs.
- **S2 (research):** overnight global cues (US peers, commodities, policy)
  → Indian open, sector-level.
- **S3 (baseline, expected to fail after costs):** short-horizon mean
  reversion or pairs. Used to validate the backtester.
- **Rejected:**
  - HFT or latency arbitrage (infrastructure plus SEBI limits),
  - crypto market making and arbitrage (tax, TDS),
  - naked option selling (a risk premium with tail risk, not an edge; SEBI
    found 91% of individual F&O traders lost money in FY25).

## Architecture principles

1. **The same strategy code runs in backtest, paper and live.** Only the feed
   and the broker are swapped.
2. **Point-in-time by construction.** Rows are bitemporal (`event_ts`,
   `available_at`), and research reads through `AsOf`.
3. **The append-only raw store is the source of truth.** Tables are derived
   and can be rebuilt (`reparse`).
4. **The LLM extracts facts; deterministic code decides risk and orders.**
   The risk engine has an absolute veto.
5. **Modular monolith, boring tech.** Python, SQLite/Postgres,
   Parquet/DuckDB. Split only when a measured need exists.
6. **Everything is versioned:** schema, parsers, taxonomy, prompts, models,
   cost tables and strategy params. Any past decision can be replayed.

```
Recorder 24/7 → raw store → parsers → event log (bitemporal)
   → refdata (security master, calendar, indices)
   → extraction (rules → local LLM, schema-validated)
   → features (AsOf only)
   → strategy → risk engine (veto) → broker (Sim | Paper | Live)
   → ledger → reports / alerts
```

## Gates

| Gate | Pass criteria (all required) |
|---|---|
| G0 Hypothesis | Pre-registered: rationale, counterparty, capacity, expected decay, windows, costs, splits. |
| G1 Event study (daily) | Test period: mean net abnormal return > 0, 95% CI lower bound > 0 after BH-FDR, N_test ≥ 100. |
| G1b Intraday | Same, after measured live latency (p95) and intraday costs. |
| G2 Backtest | Walk-forward OOS, full costs, ≥ 300 trades, deflated Sharpe > 0 (p < 0.05), max drawdown within the configured limit, capacity ≥ planned size. |
| G3 Paper | ≥ 2 months live paper: signal count, fill rate and slippage within tolerance of the backtest, and net expectancy CI not below zero. |
| G4 Live pilot | **Human decision only.** Tiny capital, hard loss budget. |
| G5 Scale | Human decision. Only while the live expectancy CI stays > 0. |

**Kill criteria in production:** auto-disable a strategy when drawdown
exceeds its limit, the rolling expectancy CI goes negative, or slippage
exceeds the model beyond tolerance.

## Capital policy

$5 can't be tested meaningfully; fees and minimum lots dominate. Build and
validate for ₹0 first. Size positions from the *validated* edge (notional =
target net profit ÷ net edge per trade), use fractional Kelly at most, and
cap everything by the risk engine. The withdraw-profits rule controls risk
once there is proven edge; it doesn't create returns.

## Research integrity

- Pre-register, use the holdout once, count every trial, and correct for
  multiple testing.
- An LLM can know historical outcomes from its training data, so LLM-derived
  features must be evaluated on data after the model's training cutoff, or
  restricted to fact extraction and compared against a baseline that uses no
  LLM.
- Report negative results in full.
