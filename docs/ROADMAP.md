# GATS Roadmap

Each task has an ID, dependencies, a deliverable and **acceptance criteria**.
A task is done only when its criteria are met, tests are green and the work
is committed.

**Tags:**
- `HUMAN`: needs Chaitanya (an account, a credential, hardware time, a
  judgement call).
- `[ASK]`: present a plan and wait for approval.
- `G*`: a decision gate. Stop and report.

Order within a milestone is the default; independent tasks may be reordered
when one is blocked.

---

## M1: Data recorder (0.1.x) — core DONE

The recorder already captures BSE/NSE announcements, attachments, NSE EOD +
delivery data, price bands and instruments. It also has point-in-time
storage, `probe`/`backfill`/`reparse`/`inspect-bad`, BSE throttle retries,
per-day backfill bookkeeping and the `reconcile` job.

- **T1.1 Bootstrap & health check.**
  - Work: venv, install, `gats init`, tests green, `gats version` = 0.1.2.
    Write a short PROGRESS entry on the environment found (Python version,
    OS, whether `data/` exists).
  - Accept: suite green on the user's machine.
- **T1.2 Real fixtures.** Depends on T1.1.
  - Work: run `gats probe` for bse, nse, eod, bands and instruments. Save
    trimmed samples (≤ 30 rows, anonymised if needed) to
    `tests/fixtures/real/`, and add tests that parse them with coverage
    assertions.
  - Accept: tests pass offline on real formats.
- **T1.3 NSE extra fields.** Depends on T1.2.
  - Work: inspect `dt`, `difference` and other unmapped NSE fields in real
    samples. Map them only if their meaning is evidenced (e.g. compare with
    `an_dt`/`exchdisstime`), bump `nse.PARSER_VERSION`, `reparse nse_ann`.
  - Accept: the mapping is documented in DATA_SOURCES.md with evidence.
- **T1.4 History depth.** Depends on T1.1.
  - Work: find how far back each source goes. Probe single days at 1 month,
    1, 2, 3, 5 and 8 years ago for BSE announcements, NSE announcements and
    NSE `sec_bhavdata_full`. Record the results in DATA_SOURCES.md.
  - Don't build parsers for older formats yet; note them.
  - Accept: a table of earliest verified dates per source.
- **T1.5 Status UX.** Depends on T1.1.
  - Work: `gats status` flags problems in plain words (job failing > 30 min,
    heartbeat stale, disk > N GB, BSE failures) and shows the reconcile
    queue size. Add a `gats doctor` command that runs health checks and
    prints fixes.
  - Accept: tests cover every warning.
- **T1.6 GitHub + CI.** `HUMAN`
  - Work: ask the user to create a private repo and authenticate. Instructions:
    `winget install GitHub.cli`, then `gh auth login`.
  - Then: add the remote, push, and confirm CI is green on Ubuntu and Windows.
  - Accept: CI green link recorded in PROGRESS.

---

## M2: Reference data & entity resolution (0.2.0)

Goal: every announcement maps to one security, point-in-time, across NSE and
BSE, with a trading calendar and benchmark indices.

- **T2.1 BSE scrip master.**
  - Work: find a verifiable source mapping BSE scrip code → ISIN (+ symbol,
    group, status). Candidates to **probe** (unverified): bseindia.com's
    "List of Scrips" API, and BSE's daily bhavcopy (newer formats include
    ISIN).
  - Implement source + parser + `bse_instruments` snapshot table + a daily
    recorder job.
  - Accept: ≥ 95% of BSE announcement scrip codes from the last 30 days
    resolve to an ISIN.
- **T2.2 NSE symbol history.**
  - Work: find a verifiable source of symbol changes (candidate, unverified:
    NSE archives `symbolchange.csv`). Store history with effective dates.
  - Accept: a test with a real historical rename resolves both old and new
    symbols.
- **T2.3 Security master.**
  - Work: `securities(security_id, isin, name, …)` plus
    `security_identifiers(security_id, id_type, value, valid_from, valid_to,
    source)`, and a `refdata.resolve(id_type, value, at)` point-in-time
    resolver. Handle ISIN changes and symbol changes.
  - Accept: unit tests for validity windows; resolution is PIT-safe.
- **T2.4 Link announcements.**
  - Work: an `announcement_security` link table filled by a batch command and
    kept current by the recorder.
  - Accept: coverage report `gats refdata coverage` shows ≥ 98% of NSE and
    ≥ 95% of BSE equity announcements resolved; unresolved ones are listed.
- **T2.5 Cross-exchange dedupe.**
  - Work: group the same filing across BSE and NSE into `event_group_id`
    (same security, compatible category, dissemination within a configurable
    window, text similarity). The earliest dissemination defines event time.
  - Accept: precision is checked by hand on 50 sampled groups and the result
    written to PROGRESS. `HUMAN`: optional spot check.
- **T2.6 Trading calendar.**
  - Work: derive trading days from EOD availability (404 = holiday), plus a
    verified holiday list if a source exists. Provide `calendar.is_trading_day`,
    `next_session_open(t)` and `session_close(d)` (09:15–15:30 IST;
    special sessions come from data).
  - Accept: tests around weekends, holidays and midnight boundaries.
- **T2.7 Benchmark indices.**
  - Work: daily closes for Nifty 50, Nifty 500 and the main sectoral indices
    (candidate, unverified: NSE archives `ind_close_all_DDMMYYYY.csv`), plus
    an `index_eod` table, recorder job and backfill.
  - Accept: probe verified; a year backfilled on a small test range.
- **T2.8 Corporate-action safety.**
  - Work: verify on real data whether NSE `PREV_CLOSE` is adjusted on
    ex-dates, using ≥ 3 known splits or bonuses. If it is, define the daily
    return as `CLOSE/PREV_CLOSE − 1` (split-safe). If not, ingest corporate
    actions from a verified source and build adjustment factors.
  - Accept: a documented finding plus a test that uses the real cases.
- **T2.9 Surveillance lists.**
  - Work: find verifiable sources for ASM/GSM lists and trade-to-trade
    (T2T/BE series) flags. Add daily snapshots, which the risk engine will
    use.
  - Accept: probe verified, job running, history starting today.
- **M2 wrap-up:** version 0.2.0 and `docs/learning/M2.md`.

---

## M3: Event study, the kill test (0.3.0)

Goal: find out whether **any** disclosure type predicts abnormal returns that
are **tradeable after the event became available**, net of costs, out of
sample. Daily data only.

- **T3.0 Backfill plan.** `[ASK]` `HUMAN`
  - Work: estimate requests and hours per source for ≥ 3 years (BSE at 2–4
    s/request, about 25 pages/day), then give the exact commands. Suggested
    order: EOD and indices first (small), then NSE announcements (1
    request/day), then BSE overnight in 1-year chunks.
  - The user runs them in a separate terminal. Monitor with `gats status`.
  - Accept: `backfill_days` complete ≥ 98% for the range; gaps are listed.
- **T3.1 Event taxonomy v0.**
  - Work: `configs/event_taxonomy.yaml` (versioned) maps
    source/category/subcategory/subject patterns to `event_type`:
    ORDER_WIN, RESULTS, RATING_UP, RATING_DOWN, BUYBACK, BONUS_SPLIT,
    DIVIDEND, ACQUISITION, FUNDRAISE, PLEDGE_CHANGE, PROMOTER_BUY,
    MGMT_CHANGE, PENALTY_LITIGATION, AGM_NOISE, OTHER.
  - Add an `event_types` table with the taxonomy version, and a coverage
    report of the top unmapped patterns.
  - Accept: ≤ 10% of announcements (excluding AGM_NOISE) map to OTHER.
- **T3.2 Pre-registration.**
  - Work: write `docs/research/M3_prereg.md` before any return is computed:
    - hypotheses per event type (with direction),
    - the entry rule: first session open strictly after `available_at`,
    - exits: entry-day close, +1, +3, +5 sessions,
    - benchmark: Nifty 500, or sector index where mapped,
    - filters: 20-day median traded value ≥ configurable ₹ threshold, skip
      entries near circuit limits, skip ASM/GSM/T2T where known,
    - costs: delivery vs intraday cost model; until M6, a conservative flat
      round-trip % documented as an assumption,
    - train/test split dates,
    - pass criteria (below) and the BH-FDR correction.
  - Accept: committed before T3.3's first run.
- **T3.3 Event-study engine.**
  - Work: `gats.research.event_study`, with all reads through `AsOf` and the
    calendar. Output a per-event frame (Parquet) with raw, benchmark and
    abnormal returns per window, costs and filters applied. CLI:
    `gats research event-study --config ...`.
  - Accept: leak tests (moving any event's `available_at` later can never
    make its entry earlier), plus unit tests on synthetic data with a known
    planted effect that the engine recovers.
- **T3.4 Statistics & report.**
  - Work: per event type × liquidity bucket, report:
    - N, mean and median net abnormal return, hit rate,
    - t-stat with date-clustered SE, bootstrap 95% CI,
    - train vs test, BH-adjusted p-values,
    - CAR plots (matplotlib → PNG).
  - Write `reports/M3_event_study.md`.
  - Accept: the report regenerates from one command; numbers are
    spot-checked by hand on 3 events.
- **T3.5 G1 decision.** `G*`
  - Work: write `docs/research/M3_decision.md`. An event type **passes** only
    if **test-period** mean net abnormal return > 0 with the CI lower bound
    > 0 after correction, and N_test ≥ 100.
  - If none pass: record it, propose next hypotheses (DESIGN §Backlog) and
    **stop for the human**.
  - If some pass: list them. They define M4's scope.
- **M3 wrap-up:** version 0.3.0 and `docs/learning/M3.md`.

---

## M4: LLM extraction (0.4.0)

Scope: the event types that passed G1. If none passed, do M4 only on the
human's instruction (ORDER_WIN is the default candidate).

- **T4.1 Attachment text.**
  - Work: extract text with `pypdf` into a `document_texts` table
    (doc_id, extractor, version, pages, chars, text). Flag scanned PDFs
    (`needs_ocr`); OCR is out of scope unless the human asks.
  - Also backfill the attachments that were skipped by policy, for the
    in-scope event types.
  - Accept: ≥ 90% of in-scope PDFs yield text.
- **T4.2 Schemas.**
  - Work: pydantic output models per event type. Example, OrderWin:
    `amount`, `currency`, `amount_inr`, `amount_text`, `counterparty`,
    `domestic_or_export`, `duration_months`, `is_repeat_order`,
    `confidence`, `evidence_span`. Add unit normalisation (₹/Rs/INR,
    crore/lakh/million/billion).
  - Accept: normalisation tests on ≥ 30 real phrasings.
- **T4.3 Rules baseline.**
  - Work: a regex/heuristic extractor for amounts and counterparties.
  - Accept: evaluated in T4.6.
- **T4.4 Local LLM extractor.** `HUMAN`
  - Human: install Ollama and pull a Qwen instruct model; you give the
    commands, the user confirms `ollama list`.
  - Work: call Ollama with JSON-schema structured output at temperature 0.
    Version prompts by hash. Cache results by (doc_id, prompt_hash, model).
    Cascade: rules → LLM when rules are unsure.
  - Accept: runs on 20 docs end-to-end; invalid JSON is rejected and
    logged.
- **T4.5 Labelled eval set.** `HUMAN`
  - Work: sample about 300 in-scope filings, stratified. The LLM pre-labels
    them. The human reviews and corrects with a simple `gats label` CLI
    (shows excerpt + proposed JSON; accept/edit/skip), stored in
    `labels/*.jsonl` (committed).
  - Accept: ≥ 300 human-verified labels.
- **T4.6 Extraction metrics.**
  - Work: per-field accuracy, amount within 1%, abstention rate, and
    rules vs LLM vs cascade, in `reports/M4_extraction.md`.
  - Accept: cascade amount accuracy ≥ 90%, or a documented plan (better
    prompt / LoRA fine-tune = optional T4.8, `HUMAN` GPU).
- **T4.7 Magnitude features + re-test.**
  - Work: point-in-time `amount_vs_revenue` (revenue from the latest results
    before the event; the source needs verification), with a documented
    fallback proxy if unavailable.
  - Pre-register a new study (e.g. ORDER_WIN with ratio ≥ X), then run it
    once on test data.
  - Accept: a decision report, like G1.
- **M4 wrap-up:** version 0.4.0 and `docs/learning/M4.md`.

---

## M5: Intraday data & reaction curves (0.5.0)

- **T5.1 Broker market-data adapter.** `HUMAN`
  - Human: create a developer app (default: Upstox) and add its keys to
    `.env`; you list the exact key names and the daily-token steps from the
    broker's official docs.
  - Work: implement `marketdata/` (instrument master, historical 1-minute
    candles, rate limits) against **verified** docs. Store bars as Parquet
    (`data/bars/1m/…`), queried with DuckDB.
  - Accept: one symbol-day probe matches the broker's chart.
- **T5.2 Event-window bars.**
  - Work: fetch 1-minute bars only for event days ±1 session, for the
    in-scope events plus the index. Resumable.
  - Accept: coverage ≥ 95% of in-scope events since the data start.
- **T5.3 Reaction curves.**
  - Work: returns from dissemination to +1/+5/+15/+30/+60 min, close and
    next close, benchmark-adjusted. Use **our measured live latency**
    (p50/p95 from `gats status`) to compute the *tradeable remainder*.
    Write `reports/M5_reaction_curves.md`.
  - Accept: the report is reproducible.
- **T5.4 G1b decision.** `G*`
  - Work: pre-registered. The intraday strategy passes only if its
    out-of-sample net expectancy after latency and costs has CI > 0.
    Otherwise stop.
- **M5 wrap-up:** version 0.5.0 and `docs/learning/M5.md`.

---

## M6: Backtester (0.6.0)

- **T6.1 Cost model.**
  - Work: `configs/costs/india_equity.yaml`, versioned with effective dates.
    Cover brokerage (the user's broker), STT (intraday vs delivery), exchange
    transaction charges (NSE/BSE), SEBI fee, stamp duty, GST and DP charges.
  - **Every value** needs an official source URL and a date checked.
  - Accept: tests reproduce ≥ 3 examples from the broker's official
    brokerage calculator (`HUMAN` may provide screenshots).
- **T6.2 Strategy interface.**
  - Work: `Strategy.on_event(...)` and `on_bar(...)` → signals. Pure,
    deterministic, with parameters from YAML and a version hash.
  - Accept: the same object runs in the backtest, paper and live runtimes.
- **T6.3 Engine + SimBroker.**
  - Work: an event-driven replay (events + 1-minute bars).
    - Limit orders with a protection band.
    - Fills at the next bar after latency.
    - Volume participation cap; slippage model by liquidity.
    - No fills at circuit limits.
    - Intraday square-off at 15:20 IST; T+1 for delivery.
  - Accept: unit tests per fill rule, plus a golden test on a tiny dataset.
- **T6.4 Risk engine** (shared with live).
  - Work: rules for max position % equity, max open positions, max daily
    loss, per-symbol cap, min liquidity, ASM/GSM/T2T block, orders/sec < 10
    (SEBI threshold for unregistered retail algos), stale-data halt, and a
    kill-switch file. Deterministic, with no LLM.
  - Accept: a test for every rule, including the veto path.
- **T6.5 Ledger & metrics.**
  - Work: trades, net PnL, equity curve, drawdown, Sharpe/Sortino, turnover,
    capacity estimate, and a tax-category summary (informational only).
  - Accept: reconciles to the sum of fills to the paisa.
- **T6.6 Experiment registry.**
  - Work: log every run (params hash, data window, git SHA, metrics) to
    `experiments`. Add `gats experiments list`. The trial count feeds the
    deflated Sharpe.
  - Accept: runs can't execute without being logged.
- **T6.7 Validation.**
  - Work: walk-forward with purged/embargoed splits, deflated Sharpe,
    holdout discipline, and a property-based leak test (future data can't
    change past decisions).
  - Accept: the leak test catches a deliberately planted leak.
- **T6.8 G2 decision.** `G*`
  - Work: write `reports/M6_backtest.md` against the pass criteria in
    DESIGN §Gates. Stop for the human.
- **M6 wrap-up:** version 0.6.0 and `docs/learning/M6.md`.

---

## M7: Paper trading (0.7.0)

- **T7.1 Live runtime (paper).**
  - Work: new events from the recorder (low-latency hand-off) → features →
    strategy → risk → PaperBroker using live quotes from the broker API
    (`HUMAN` keys) → ledger.
  - Accept: runs a full session on the mock exchange and on one real day in
    paper mode.
- **T7.2 Latency budget.** `[ASK]`
  - Work: measure the end-to-end latency distribution and propose poll
    intervals that stay polite. The human approves any change.
- **T7.3 Alerts.** `HUMAN`
  - Work: Telegram bot (the human creates the bot and puts the token in
    `.env`). Send signals, fills, errors and a daily summary.
- **T7.4 Ops.** `HUMAN`
  - Work: move the recorder and runtime to an always-on Linux VM, plus
    Postgres, systemd, backups and monitoring. You write scripts and docs;
    the human provisions the VM.
- **T7.5 G3 decision.** `G*`
  - Work: after ≥ 2 months of paper trading, compare against the backtest
    (signal counts, fill rates, slippage, net expectancy) and write
    `reports/M7_paper.md`. Stop for the human.
- **M7 wrap-up:** version 0.7.0 and `docs/learning/M7.md`.

---

## M8: Live pilot (0.8.0), activated by the human only

- **T8.1 Order adapter + OMS.**
  - Work: broker order API (verified docs), an order state machine,
    idempotent client order IDs, reconciliation against broker positions,
    and market protection on every order. **Disabled by default.**
- **T8.2 Gate enforcement.**
  - Work: `gats live` refuses to start without a passed G3 and a
    `live_approval` record created by `gats gate approve`.
    `gats gate approve` must require an interactive TTY and a typed
    confirmation that shows the capital cap and loss limits.
  - Accept: tests prove both refusals. You never run either command.
- **T8.3 Hard caps.**
  - Work: max capital, max daily loss and max position caps in config, with
    auto-disable on a breach, a kill switch, and a Telegram alert.
- **T8.4 Runbook.**
  - Work: `docs/RUNBOOK_LIVE.md` covering static IP (SEBI requirement),
    broker API registration, start/stop, incident response and daily
    checks. `HUMAN` does everything that involves accounts or money.
- **G4:** the human decides. Nothing in this milestone is switched on by you.
