# Progress

Claude Code updates this after every task. Newest log entry first.

## Status

- **Version:** 0.2.0 · **Schema:** v11 · **Milestone:** M3 (M1 waits only on T1.6, HUMAN)
- **Next up:** VERIFY FIRST (next session), then the open items below. M4 to M8 are code-complete; every real run waits on the human. Open code items, in order: (a) T7.1 acceptance: a full session over `scripts/mock_exchange.py` (the in-process full-session test passes; the mock exchange does not serve candles or PDFs yet); (b) a recorder job that keeps quarterly results fresh (today only `gats backfill results`; stale revenue makes paper differ from a later backtest); (c) filings on non-trading days are not traded by the engine in backtest or paper (decide before the G1b pre-registration); (d) learning notes M3 to M8; (e) `run_live` has only been tested in parts. Waiting on the human: recorder (1), M3 run (3), labels (5), Upstox token (6), items 8 to 12

## Waiting on the human (HUMAN)

Do these in order; each is independent of Claude's ongoing work. Commands
are PowerShell, run from `C:\Projects\GATS`.

1. **Start the recorder** in its own terminal and leave it running (PC
   awake: Settings → System → Power → Screen and sleep → Never when plugged in):
   `powershell -ExecutionPolicy Bypass -File scripts\run_recorder.ps1`
   Check it any time with `.venv\Scripts\gats status`.
2. **T1.6 GitHub + CI.** `gh` 2.96 is installed but not logged in.
   ```powershell
   gh auth login            # GitHub.com → HTTPS → Login with a web browser
   gh repo create gats --private --source . --remote origin --push
   gh run watch             # wait for CI (Ubuntu + Windows, Py 3.11/3.12/3.14)
   ```
   Then send back the CI run URL (or type `/gats`; Claude checks `gh run list`).

3. **T3.0 Backfill plan [ASK] — approve by running it.** Range
   2019-10-01 → 2026-10-02 (EOD prices start 2019-10-01). Estimates use rates
   measured on 2026-10-03: NSE archives 1.0 s/request, NSE announcements
   1.6 s/day, BSE 3.95 s/page at 4 s spacing (~25 pages/day). Disk: ~4 GB for
   phase A, ~10 GB with BSE; 82 GB free.

   **Phase A (recommended, ~2.5 h, fine while the recorder runs).** Run in a
   second terminal, one after another; each is resumable (rerun to continue):
   ```powershell
   cd C:\Projects\GATS
   .venv\Scripts\gats backfill eod --start 2019-10-01 --end 2026-10-02          # ~42 min
   .venv\Scripts\gats backfill indices --start 2019-10-01 --end 2026-10-02      # ~37 min
   .venv\Scripts\gats backfill announcements --source nse --start 2019-10-01 --end 2026-10-02   # ~70 min
   .venv\Scripts\gats refdata build        # links the new filings
   .venv\Scripts\gats refdata dedupe
   .venv\Scripts\gats status               # check 'backfill days' and health
   ```
   **Phase B (optional, BSE, ~10 h per year with the recorder running).** The
   M3 study prices on NSE, so NSE filings suffice; BSE adds earlier
   timestamps (minutes) and BSE-only companies (not priced in M3). If wanted,
   one year per night, newest first:
   ```powershell
   $env:GATS_HOST_MIN_INTERVAL_S='{"api.bseindia.com": 4}'
   .venv\Scripts\gats backfill announcements --source bse --start 2025-10-01 --end 2026-08-31
   .venv\Scripts\gats backfill announcements --source bse --start 2024-10-01 --end 2025-09-30
   # ... and so on back to 2019-10-01; then: gats refdata build; gats refdata dedupe
   ```
   Then type `/gats`: Claude checks completeness (≥ 98% of days `complete`,
   gaps listed) and runs the pre-registered M3 study (T3.4 → G1).

4. **Optional (T2.5):** spot-check a few of the 50 pairs in
   `docs/research/dedupe_v1_review.md` (each shows both exchanges' text) and
   tell Claude if any verdict looks wrong.

5. **T4.5 Label at least 300 order wins** (about 30 s each, any number of
   sittings). Ollama must be running: the LLM proposes half of the items.
   Read `labels\README.md` first (one page).
   ```powershell
   cd C:\Projects\GATS
   .venv\Scripts\gats label prepare   # optional, ~1.5 h unattended: the LLM answers every item in advance (resumable)
   .venv\Scripts\gats label review    # label; q stops, run it again to continue
   .venv\Scripts\gats label stats     # progress; the target is 300 labelled
   ```
   Then type `/gats`: Claude commits `labels\order_win_v1.jsonl` and runs
   T4.6 (`gats extract evaluate`: rules vs LLM vs cascade, `reports\M4_extraction.md`).

6. **T5.1 Upstox Analytics Token** (5 minutes; needs an Upstox account).
   It is read-only (it cannot trade), valid for a year, and needs no daily
   login. Open https://account.upstox.com/developer/apps#analytics, go to
   the **Analytics** tab, click **Generate Token**, confirm, and copy the
   whole token with the clipboard icon. Add one line to `C:\Projects\GATS\.env`
   (Claude never reads that file):
   ```
   GATS_UPSTOX_ANALYTICS_TOKEN=<paste the token>
   ```
   Then type `/gats`. Claude probes the token, checks the cost model against
   Upstox's own brokerage calculator (T6.1), checks a day of bars against
   NSE's own end-of-day file, checks that a stock whose ISIN changed still has
   its older bars, and then gives you one resumable command for the full
   event-window fetch (`gats bars events`, about 1–2 h at Upstox's rate limit).

7. **T4.7 Quarterly results (company revenue).** Run after item 3 and
   `gats refdata build` (it asks about the companies that have order-win
   filings: 281 today, more after the full backfill). About 1 second per
   file, so roughly 2 h now and several hours after the backfill; resumable,
   and fine while the recorder runs (NSE only):
   ```powershell
   cd C:\Projects\GATS
   .venv\Scripts\gats backfill results --type ORDER_WIN --since 2021-01-01 --limit 20000
   ```
   Rerun the same command until it prints `XBRL: 0 read`.

8. **T7.5 Confirm the G3 criteria BEFORE paper trading starts.** Open
   `configs/g3.yaml` (five numbers Claude proposed: 60 days, 30 trades,
   signals within 20%, fill rate within 10 points, slippage within 10 bps).
   Change them now if you disagree and tell Claude; once the paper run has
   produced data they are fixed.

9. **T7.3 Telegram alerts** (5 minutes). In Telegram, talk to `@BotFather`,
   send `/newbot`, follow it, copy the token. Send any message to your new
   bot, then open `https://api.telegram.org/bot<token>/getUpdates` in a
   browser and copy `chat.id`. Add to `C:\Projects\GATS\.env`:
   ```
   GATS_TELEGRAM_BOT_TOKEN=<token>
   GATS_TELEGRAM_CHAT_ID=<chat id>
   ```

10. **T7.1 One real day of paper trading** (after items 1 and 6, on a
    trading day). First measure the live candle feed (run it a few times
    just after a minute turns, once before 09:15, once after 15:30, and send
    Claude the output):
    ```powershell
    cd C:\Projects\GATS
    .venv\Scripts\gats probe upstox-intraday --symbol RELIANCE
    ```
    Then start the paper run in its own terminal, next to the recorder:
    ```powershell
    powershell -ExecutionPolicy Bypass -File scripts\run_paper.ps1 -Name s1
    .venv\Scripts\gats paper status      # any time, in another terminal
    ```
    It cannot send an order. `New-Item data\KILL` stops new entries.

11. **T7.4 Always-on VM.** `docs/OPS.md` has the steps (provision, move
    `data\`, `.env`, two systemd units, nightly backup). Two months of paper
    trading on a laptop that sleeps is two months of gaps.

12. **M8 Live pilot: yours alone.** `docs/RUNBOOK_LIVE.md`. Nothing is
    switched on. Claude never runs `gats live` or `gats gate approve`, and
    never sets the caps in `configs/live.yaml`.

## Blocked

(none)

## Milestones

- [x] M1 core recorder (0.1.0 → 0.1.2)
- [ ] M1 finishing: T1.1 ✅ · T1.2 ✅ · T1.3 ✅ · T1.4 ✅ · T1.5 ✅ · T1.6
- [x] M2 Reference data & entity resolution (0.2.0): T2.1 ✅ · T2.2 ✅ · T2.3 ✅ · T2.4 ✅ · T2.5 ✅ · T2.6 ✅ · T2.7 ✅ · T2.8 ✅ · T2.9 ✅
- [ ] M3 Event study (G1 kill test): T3.0 ⏳ (human) · T3.1 ⚠️ · T3.2 ✅ · T3.3 ✅ · T3.4 ✅ (code; real run ⏳ backfill) · T3.5 ⏳ (gate, after the real run)
- [ ] M4 LLM extraction (infrastructure for ORDER_WIN; scope confirmed at G1): T4.1 ✅ · T4.2 ✅ · T4.3 ✅ · T4.4 ✅ · T4.5 ⏳ (code ✅; labels: human) · T4.6 ⏳ (code ✅; needs the labels) · T4.7 ⏳ (pre-registered; code ✅; the run needs items 3, 5 and 7)
- [ ] M5 Intraday data & reaction curves (G1b): T5.1 ⏳ (code ✅; token: human, item 6) · T5.2 ⏳ (code ✅; the fetch needs the token) · T5.3 ⏳ (pre-registered; code ✅; the run needs bars and recorder latency) · T5.4 ⏳ (gate)
- [ ] M6 Backtester (G2): T6.1 ⏳ (model ✅; broker-calculator check needs the token, item 6) · T6.2 ✅ · T6.3 ✅ · T6.4 ✅ · T6.5 ✅ · T6.6 ✅ · T6.7 ✅ · T6.8 ⏳ (code ✅; the G2 run needs bars, events and T4.7's revenue feature)
- [ ] M7 Paper trading (G3): T7.1 ⏳ (code ✅; mock-exchange session open; the real day: human, item 10) · T7.2 ⏳ [ASK] (measured by `gats paper status`; needs live data) · T7.3 ⏳ (code ✅; bot: human, item 9) · T7.4 ⏳ (scripts and docs ✅; VM: human, item 11) · T7.5 ⏳ (report code ✅; needs two months of paper)
- [ ] M8 Live pilot (G4, human-only): T8.1 ✅ (off by default; order API documented, never called) · T8.2 ✅ · T8.3 ✅ · T8.4 ✅ · G4: the human's

## Tasks done

- M1: recorder, point-in-time schema, raw store, probe/backfill/reparse
  (0.1.0)
- M1: BSE one-day queries, attachment storage policy (0.1.1)
- M1: BSE throttle retries, `backfill_days` (schema v2), reconcile job,
  deeper catch-up, `inspect-bad` (0.1.2)

## Log

- **2026-10-04:**
  - M8 code (T8.1 to T8.4), all off. Three locks with no bypass: `GATS_LIVE_ENABLED`
    (false), a human's approval row bound to the design hash, the caps hash and the hash
    of a G3 report that says PASS (`gats gate approve`: interactive terminal and a typed
    sentence naming the capital at risk), and hard rupee caps in `configs/live.yaml`
    (shipped as 0 = not set; the human's numbers). Order layer: intent stored before
    sending, one key per engine order (never sent twice), a request with no answer is
    never resent and switches trading off, fills reconciled with the broker's positions.
    Upstox order API read from the docs, never called; LIMIT orders only. Tests prove
    `gats live` and `gats gate approve` refuse. `docs/RUNBOOK_LIVE.md`.
  - T7.5 G3 report (`gats paper report`): the run against a backtest of its own filings
    and bars, criteria in `configs/g3.yaml` fixed beforehand; writes the `**G3: PASS**`
    line the live gate reads. T7.3 alerts (Telegram; token kept out of logs). T7.4
    `docs/OPS.md`, `deploy/gats-paper.service`, `scripts/run_paper.ps1`, `scripts/backup.sh`.
  - T7.1 paper runtime: live bars (Upstox intraday candles, documented, unprobed), live
    events through the backtest's own code, the run loop, `gats paper run|status`,
    `gats probe upstox-intraday`. A full fake-clock session passes (real PDF, entry,
    timed exit, day close, restart mid-session identical). The recorder's hand-off job
    takes a new order win to readable text at once.
  - Not verified by a real run (next session and human steps): anything against Upstox
    with a token, a real trading day, the live order path.

- **2026-10-03:**
  - T7.1 (part 1) the paper run's journal. The paper runtime is the backtest engine fed
    live, so the engine now takes one item at a time with the moment it was really handed
    over (`step(item, at)`): a late candle decides late and cannot fill in bars that went
    by; handing an item over before it is known is refused. `paper_journal` (schema v10)
    holds every input in order; replaying it rebuilds the account, so a restart needs no
    state snapshot. Orders and fills are stored as they happen and a replay must reproduce
    them: changed code is refused instead of rewriting a paper record. What the risk rules
    were told (liquidity, surveillance, kill switch) is written next to each step and
    replayed from there. A run belongs to one design hash.
  - T4.7 pre-registered and coded (`docs/research/M4_prereg.md`, hash pinned, committed
    before any return for order wins existed). M3's study with one registered filter:
    order value / trailing revenue >= 10%, both as of the filing; 1%, 5%, 20% reported
    only. It reads M3's test period a second time, so BH runs at q = 0.025. `gats research
    magnitude` refuses an edited config, incomplete data, or a missing T4.6 evaluation
    (the report quotes the measured extraction accuracy).
  - T4.7 revenue feature done (the backfill is the human's, item 7). `financial_results`
    (schema v9): one row per results filing with its dissemination time; `gats backfill
    results` stores both indexes and reads each quarter's revenue from XBRL only when the
    file matches its filing (quarter end, standalone/consolidated), consolidated first.
    `AsOf.trailing_revenue`: four latest consecutive quarters public by the moment asked,
    revisions counted from when they were public, no answer for gaps or stale figures.
    Events get `amount_vs_revenue` in the backtest feed. Real check: 281 companies with
    order wins; 3i Infotech June 2026 quarter Rs 177.94 cr consolidated.
  - T4.7 revenue source VERIFIED (ingestion next). NSE's results come in two regimes: the
    legacy index (2005 → Oct-Dec 2024, where it stops) and the integrated-filing index (from
    the March 2025 quarter). Both give dissemination times (so trailing revenue can be
    point-in-time) and an XBRL whose `RevenueFromOperations` in context `OneD` is the
    quarter's revenue in rupees. Trap found in the real file: the legacy `FourD` context
    carries nine months under the quarter's dates. `gats.sources.nse_results`, four real
    fixtures.
  - T5.3 pre-registered (`docs/research/M5_prereg.md`, config hash pinned, committed before
    any bar or intraday return existed) and coded. `gats.research.reaction`: entry at the
    first bar after availability + *measured* feed latency (p95 of our live recording; the
    run is refused without 500 live filings) + fixed allowances; exits at 5/15/30/60 min
    and the 15:20 square-off; Nifty 500 minute bars as the benchmark; costs from the
    verified model for a Rs 50,000 round trip + 5 bp slippage per side. G1b is judged by
    M3's gate code unchanged. `gats research reaction --scope ...` refuses an edited
    config, bar coverage under 95% or unmeasurable latency. Tested: a longer delay never
    enters earlier; nothing before the entry bar changes a return.
  - T4.6 code done (it needs the labels, item 5). `gats extract evaluate` scores rules, LLM
    and cascade on the labelled new orders: amount within 1% with a Wilson interval, misses
    split into abstained and wrong, counterparty, domestic/export, duration, repeat; the
    share of labelled items that are new orders (taxonomy precision); and the anchoring
    check (accuracy where an extractor's own proposal was shown vs not). It refuses to
    score if the LLM gives no answer, and states the 90% acceptance mechanically.
  - T6.8 groundwork done (the G2 run itself waits on data). `gats.backtest.feed` (events
    with extracted facts, bars from the Parquet files, liquidity and surveillance lookups
    that read only before the day asked about; surveillance is "unknown" before its
    recorded history, never "clear"), `gats.backtest.report` (G2's criteria from DESIGN,
    judged mechanically; a non-holdout run is labelled NOT A G2 RUN) and `gats backtest
    run`. Tested end to end on a synthetic market. Found there: a trade size equal to the
    10% position cap is always refused, because risk sizes at the worst-case limit price;
    the default is now Rs 50,000. S1 trades nothing on real data until T4.7 supplies
    order value / revenue.
  - T6.7 done. `gats.backtest.validation`: walk-forward splits with embargo and purging;
    probabilistic and deflated Sharpe (Bailey & Lopez de Prado 2014; stdlib NormalDist; the
    expected maximum of 100 unskilled trials reproduces the known 2.51 sd), with trials and
    their Sharpe spread read from the registry; a leak detector that changes the future
    after random cuts and requires identical decisions up to each cut (S1 passes; a
    planted five-minute look-ahead is caught). Holdout backtests are refused, before
    anything is computed or logged, unless a pre-registration records the design hash.
  - T6.6 done. `experiments` table (schema v8) and `gats.research.registry.experiment`: the
    row is written before the run starts, so crashed and abandoned runs still count;
    trials = distinct designs (params hash), so a rerun is a reproduction. Backtests run
    only through `gats.backtest.runner.run_backtest`, which registers first (if the row
    cannot be written the strategy is never called: tested); its design hash covers
    strategy, costs, risk limits and engine settings, with the git commit recorded. The
    M3 event study registers too. `gats experiments list` shows runs and the trial count.
  - T6.5 done. `gats.backtest.ledger`: FIFO trades with both sides' charges pro rata; the
    trades' net P&L less open lots equals the fills' cash flow to the paisa (tested on the
    golden run and on a run ending with shares held and unsettled cash). Metrics: net/gross
    P&L, win rate, profit factor, turnover, drawdown, Sharpe/Sortino (idle sessions count
    as flat days when the calendar is passed, so sparse event days cannot inflate them),
    capacity headroom under the participation cap, and informational tax buckets (12-month
    rule from the Income Tax Department). Not modelled: tick-size rounding of fills (tick
    sizes are unverified).
  - T6.4 done. `gats.risk.engine` + `configs/risk.yaml`: order rate (5/s; the exchange
    threshold is 10, NSE/INVG/67858), kill-switch file, daily loss, open positions,
    position % of equity, per-symbol cap, liquidity floor, ASM/GSM/T2T block, stale data.
    It sees neutral snapshots (order intent, account exposure), so backtest, paper and
    live share it; the first refusal names the rule. Exits pass everything but the order
    rate; unknown liquidity fails closed. The limits are conservative defaults: the human
    sets the real ones before G4. Also learned: API trading needs a static IP registered
    with the broker (a HUMAN step for M8).
  - T6.3 done. `gats.backtest.engine`: replays events and 1-minute bars in the order they
    became known and runs a strategy as live would. Pessimistic fills: latency (an order
    fills in a bar that starts after it reaches the market), protection band (a gap
    through it = no fill), 10% participation, spread + impact slippage, no fills into
    circuits (flat bars at the day's extreme count as locked when limits are unknown),
    15:20 square-off and no new intraday entries after it, T+1 delivery and settlement, no
    leverage, verified costs per fill. Golden test: S1 on a tiny two-day market. Found by
    the golden run: a stateless strategy repeated its exit until the fill; the Context now
    shows working orders and the engine drops duplicate exits. Spread, impact and band
    are assumptions for G3 paper trading to calibrate.
  - T6.2 done. `gats.strategy`: strategies are pure (no I/O, no hidden state; positions and
    the clock come through a read-only Context), so one object runs in replay, paper and
    live, and a restart or a replayed day decides the same (tested). Params from YAML,
    versioned by hash. S1 `order_win_drift` (intraday): buy large order wins (order /
    revenue >= threshold), next open for after-hours filings, skip stale or late-session
    ones, exit after N minutes or at 15:15. Parameters are placeholders until G1b's
    pre-registration.
  - Order change: T6.1 before T5.3. G1b judges intraday expectancy after costs and our
    measured live latency; neither existed (costs were a flat M3 assumption, and the recorder
    has never run), and pre-registering with placeholders would bake them in.
  - T6.1 model done (broker reconciliation waits on the token). `configs/costs/india_equity.yaml`:
    every rate from primary sources read today (NSE FA and FATAX circulars 2019-2026, NSE's
    levies page, SEBI's 2020 memo, Upstox's pages), with effective periods. `gats.backtest.costs`
    prices a fill at the latest regime by default, or at a date, and refuses dates it cannot
    vouch for (broker pricing: from today). A Rs 1 lakh intraday round trip costs ~Rs 82.7
    (0.083%). Caught by the rupees-per-crore test before commit: a 10x slip in the 2026 rate.
  - T5.2 code done (the fetch waits on the token). `gats.marketdata.windows`: each in-scope
    event's previous, event and next regular session (event session = the one containing
    availability, else the next open), months needed for the stock and Nifty 500,
    `gats bars events` (capped, resumable fetch) and `gats bars coverage` (one DuckDB scan;
    target 95%). On the current data: 777 ORDER_WIN events, none dropped, after two fixes
    found on real data: today's backfilled filings needed `refdata build` to link, and 141
    events had two ISINs valid at once (an ISIN change after a split, which the master cannot
    date). Those are settled by today's listing, the ISIN the broker keys on; whether its
    history reaches back past the change is checked once the token exists.
  - T5.1 code done (the probe waits on the token, item 6). `gats.sources.upstox` (instrument
    file VERIFIED: equity keys are ISINs; candle reply DOCUMENTED, not probed),
    `gats.marketdata` (Parquet per instrument-month, atomic merge-on-write, DuckDB reads,
    times as epoch microseconds because tz-aware conversion needs tzdata/pytz on
    Windows), `bar_months` (schema v7), `gats bars fetch|show`, and a probe that checks a
    day's bars against NSE's EOD row. Auth uses Upstox's read-only Analytics Token (1 year,
    no daily login, cannot trade) instead of the daily OAuth token.
  - T4.5 code done (labelling is the human's, item 5). Held-out sample: NSE filings of
    2026-06-01 → 2026-08-31 (backfilled and read for this; 570 documents, 562 with text), never seen while the
    rules were built. `gats label sample` drew 330 of 562 documents, stratified by
    how the rules read them (annexure 211, sentence 59, none 60), and the file is committed before
    labelling. Each item shows the rules' or the LLM's proposal at random (half each),
    so T4.6 can measure anchoring. `gats label review` saves every decision at once.
  - T4.4 done. `gats extract run --mode rules|llm|cascade`: Ollama `/api/chat` with a
    JSON-schema `format` (temperature 0, seed 0). The model copies text; code derives the
    numbers and rejects bad JSON, schema misses, amounts not in the filing and sub-lakh
    amounts. Every call is logged in `llm_extractions` (the cache: document, prompt hash,
    model; cached replies are re-validated). Real runs: 20 filings LLM-only, 20/20 valid
    JSON, 18 s/call, one "400 MW" quoted as an amount and rejected. Cascade on 120: rules
    settle 106, the LLM 14 (12 accepted). Comparing with the LLM exposed rules bugs, fixed
    in order-rules-v4: label priority (Laser Power ₹72.77 cr total, not one ₹58.92 cr
    contract), a ₹1-lakh floor (v1 called ₹1–₹217 order values in 13/400), amounts in
    words (94/363 texts), split figures (KEC ₹1,303 cr, not ₹303 cr), ranges (lower
    bound). Ollama 0.32.8 with qwen2.5-coder 7B/3B was already installed (the HUMAN step).
  - T4.3 done. `gats.extract.rules.extract_order_win`: SEBI annexure fields
    (label-to-label segments, PDF cell interleaving handled) or the order
    sentence of a covering letter; confidence 0.9 / 0.7 / 0.2 with `unsure`
    for the cascade. On all 362 real order texts: amount 82% (annexure 235,
    sentence 63, none 64), counterparty 67%, domestic/export 70%, duration
    40%. Accuracy is measured against human labels in T4.6. Found while
    measuring: a 25-year contract, so the duration cap is now 50 years.
  - T4.2 done. `gats.extract.money` (Indian and Western digit grouping;
    crore/lakh/million/billion; ₹/Rs/INR/Rupees/USD/US$/€/£/AED; crore and
    lakh imply rupees; foreign amounts are never FX-converted) and
    `gats.extract.schemas.OrderWin`, which recomputes amount, currency and
    amount_inr from the verbatim `amount_text` so an extractor cannot pass a
    wrong number. 38 real phrasings tested (target ≥ 30), mined from 1,133
    distinct amount strings in 363 real order-win PDFs.
  - T4.1 done. `gats.extract` (pypdf; PDFs inside NSE zips; `needs_ocr` for
    image-only PDFs; empty-password encryption handled; versioned
    extractor), `document_texts`, recorder job `extract`, `gats extract
    fetch|texts|coverage` (fetch bypasses the storage policy for an event
    type). Real run on the 30 days: **ORDER_WIN 524/524 downloaded
    attachments yield text (100%; target ≥ 90%)**, 7 too large (> 5 MB), 0
    need OCR. 524 downloads were only 363 unique files: companies upload
    the identical PDF to both exchanges.
  - T3.4 code done: `gats.research.stats` (CR1 clustered SE, Student-t via
    incomplete beta pinned to table values, date-cluster bootstrap, BH, FCR
    bounds) and `gats.research.report` (G1 table applying the pre-registered
    rule mechanically, all types train/test, liquidity buckets fixed before any
    run, filter accounting, CAR plot, 3-event spot-check list).
    `gats research event-study` now writes `reports/M3_event_study.md` and
    logs the trial; `gats research report --run <id>` rebuilds it. Synthetic
    checks: a planted +2% passes, no effect → "no edge found", N < 100 fails.
  - **M3 is now waiting only on data:** after phase A of the backfill
    (item 3), `/gats` runs the study once, spot-checks 3 events against the
    raw bhavcopy and writes `docs/research/M3_decision.md` (T3.5, G1).
  - Decision: while waiting for G1, build M4's infrastructure (text
    extraction, schemas, rules) for ORDER_WIN, the roadmap's default
    candidate. No research is run on it until G1 says so.
  - T3.3 done. `gats.research.event_study` (entry = first regular open
    strictly after availability; split-safe returns; market-adjusted vs Nifty
    500; filters with one reason each; Parquet output) and `gats research
    event-study`, which **refuses** an edited config (hash ≠ prereg) and
    incomplete data (< 98% of sessions/filing days), and logs every run so a
    repeat is flagged as a reproduction. Tests: planted +3% recovered,
    placebo ≈ 0, each filter, bonus inside a window, and leak tests
    (later availability never enters earlier; prices after an exit cannot
    change a trade; unavailable filings invisible).
  - T3.2 done: `docs/research/M3_prereg.md` + `configs/studies/m3_event_study.yaml`
    (hash pinned in the prereg; the engine must refuse a mismatch) committed
    before any event return was computed. 5 confirmatory long hypotheses
    (ORDER_WIN, RATING_UP, BUYBACK, BONUS_SPLIT, PRESS_RELEASE) × 4 exits =
    20 tests, BH q=0.05, FCR-adjusted CIs; train ≤ 2023-12-31, test
    2024-01-01 → 2026-09-30 used once; flat 0.50% round-trip cost assumption.
  - T3.1 done, **acceptance missed**: `configs/event_taxonomy.yaml`
    (`taxonomy-v1`, stored as `taxonomy-v1+<file hash>` so an edit is always
    a new version), `announcement_event_types`, `gats events
    classify|coverage`, recorder job `classify`. On 30 days of real filings,
    OTHER is **14.1% of non-noise filings** when each filing takes its
    cross-exchange event's best type (17.6% per filing; target ≤ 10%). The
    rest is genuinely contentless metadata ("General Updates", "Disclosure
    of material issue"): the content is only in the PDF, so M4's attachment
    text is the fix. Precision was spot-checked by hand per type and false
    positives fixed (tax orders as order wins, results *intimations* as
    results, NCD interest as fund raising); that cost ~1.3 pts of coverage
    and was worth it. M3 tests typed events only; untyped filings cannot
    bias them.
  - T3.0 plan written (Waiting on the human, item 3), with measured rates:
    phase A (EOD, indices, NSE filings) ~2.5 h; BSE optional (~10 h/year).
    Decision: M3 uses NSE availability times only, which can only delay
    entries (never look ahead); BSE is an optional extension.
  - **M2 wrap-up:** version 0.2.0 (schema v5), README updated,
    `docs/learning/M1.md` and `docs/learning/M2.md` written.
  - T2.9 done. NSE ASM (long/short term) and GSM APIs verified; versioned in
    `surveillance_versions` (truncation guard 50% for short lists; one file
    never closes the other's entries), daily job `nse_surveillance`.
    Trade-for-trade = series BE/BZ/ST/SZ (NSE legend of series), from the
    daily band file. `AsOf.surveillance(symbol)` combines them; live check:
    A2ZINFRA LTASM I, AGSTRA GSM + BZ, 3IINFOLTD BE, RELIANCE clean.
  - T2.8 done. **Finding: NSE's `PREV_CLOSE` is not adjusted on ex-dates**
    (10/10 real splits/bonuses, Sep 2025; naive returns −50%…−90%). So
    returns use share multipliers from NSE's corporate-actions API
    (verified; reports today's symbol for old actions, translated via the
    symbol history): `gats.refdata.actions.ReturnAdjuster`,
    `AsOf.return_adjuster()`, daily job `nse_corp_actions` (−30…+90 days),
    `gats backfill corporate-actions` (17,019 actions 2019→2026 loaded).
    Adjusted ex-date returns on the 10 cases: all within ±20% (max:
    ADANIPOWER +20.0% real move); Sep 2026 pipeline check: 4 ex-dates,
    naive −46%…−81% → adjusted −5%…+8%. Rights/demerger/bonus-preference
    days (391) are flagged `needs_review` for exclusion; dividends are
    parsed (`cash_per_share`) but returns are price returns.
  - T2.7 done. Index closes verified and stored (`index_eod`, `index_days`,
    recorder job `nse_indices`, `gats backfill indices`, `gats probe indices`,
    `AsOf.index_history()`). The EOD and index paths now share one generic
    daily-file ingest. One year backfilled (2025-10-01 → 2026-10-02): 248
    sessions; no 2026 listed holiday has a file; the 4 unlisted weekday
    closures are real 2025 holidays; both special sessions present. The
    calendar now also uses index-file evidence.
  - Final 30-day numbers after the full BSE backfill + reparse: linking NSE
    100% (17,084), BSE 99.6% (37,042/37,187); BSE scrip→ISIN 99.5%;
    14,369 cross-exchange pairs (≈84% of NSE filings have a BSE twin).
  - T2.6 done. `gats.refdata.calendar.TradingCalendar` (EOD evidence →
    closed days → NSE holiday list → weekends; special sessions = sessions
    on weekends/holidays, skipped by `next_session_open`); `AsOf.calendar()`.
    NSE holiday API and market hours (09:15–15:30) verified; daily
    `nse_holidays` job. Real check on Sep 2026: 22 sessions, closed weekdays
    exactly the listed holidays (14 Sep, 2 Oct).
  - **Found and fixed:** weekday holidays do not 404, NSE serves the previous
    session's file (Sundays too), so the recorder refetched holiday files
    every 30 min for 10 days; now `eod_days` bookkeeping. Weekends are
    checked too, because special sessions (Muhurat, Sunday budget day
    2026-02-01) publish files.
  - T2.5 done. Same disclosure on BSE+NSE → one event (`announcement_event_group`,
    rules `dedupe-v1` in `gats.refdata.dedupe`): attachment size (BSE exact
    bytes vs NSE display size), time gap and topic-word similarity, greedy
    one-to-one. **Hand check of 50 random pairs: 47 same disclosure, 3
    uncertain, 0 wrong (precision 94–100%)**, see
    `docs/research/dedupe_v1_review.md`. 5,302 pairs on 30 days. Recorder job
    `dedupe` (every 5 min, last 2 days); `gats refdata dedupe`;
    `AsOf.events_since()` (only members available by the clock count; leak test).
  - Found and fixed: reparse skipped rows whose payload a probe stored first
    (content-addressed raw store keeps the first kind).
  - T2.4 done. `announcement_security` links (NSE: ISIN on the event date,
    else symbol as of `first_seen_at`; BSE: scrip code). Recorder jobs
    `master_build` (daily, after 09:00 IST) and `link` (every 60 s; unresolved
    retried only after a new build; merged ids relinked). Coverage, last 30
    days: **NSE 100% (16,292/16,292), BSE 99.7% (14,244/14,285)**; every
    unresolved BSE filing is a REIT/InvIT (not equities, absent from BSE's
    Equity scrip list). T2.1 check on the same data: 99.6% of BSE filings map
    to an ISIN.
  - T2.3 done. Security master (`gats.refdata.master`): NSE rename chains +
    BSE scrips joined by ISIN (union-find) → 11,006 securities, 23k
    identifier windows; `gats refdata build|resolve`; `AsOf.security()`
    (raises for dates after the clock; `strict=True` hides links not yet
    observed). Rebuild keeps IDs stable (0 new on the 2nd build).
  - Decisions: builds upsert rows stamped with `last_build_id` instead of
    deleting; mappings first seen today are extended back to 1900 for
    research on history (documented; `available_at` keeps the honest time);
    a symbol seen with two ISINs and no recorded rename is one security (ISIN
    change), while a symbol reused after a recorded rename-away is a new one.
  - Recorder still not started on this machine (no heartbeat).
- **2026-10-02:**
  - T2.2 done. `symbolchange.csv` verified (headerless, 1,065 changes since
    1999; effective date = first session under the new symbol, checked in
    EOD files). `nse_symbol_changes` + daily job + `SymbolHistory` resolver
    (chains, point-in-time load). **Finding:** NSE's announcements API
    returns the *current* symbol/name/ISIN for old filings (2024 Zomato
    filing → ETERNAL), so backfilled NSE symbols must be resolved as of
    `first_seen_at`. Refactored refdata ingest into one generic
    fetch→store→parse→apply path.
  - T2.1 done. BSE "List of Scrips" API verified (needs browser headers +
    homepage cookies; 10,918 scrips incl. delisted). Stored as type-2
    versions (`bse_scrips` + `refdata_snapshots` log, schema v4) instead of
    daily copies (~4M rows/yr saved); daily `bse_scrips` recorder job,
    `gats refdata update|coverage`. Coverage on the 30-day backfill: see the
    T2.1 coverage entry (≥ 95% required). BSE UDiFF bhavcopy also verified
    (one day), kept for later.
  - Decision: M2 schema changes all land in v4 (additive tables); a DB
    already at v4 gains new M2 tables through create_all().
  - T1.5 done. `gats status` opens with plain-language problems + fixes
    (`gats.health`: recorder never ran / stale heartbeat, job failing > 30
    min, BSE failures/hour, data size, low disk, reconcile backlog, gave-up
    days); new `gats doctor [--network]`. Thresholds in `Settings`. Data size
    is an O(1) estimate (raw bytes + DB files) to keep status fast.
  - T1.4 done; history table in DATA_SOURCES.md. BSE and NSE announcements
    reach ≥ 2012; NSE `sec_bhavdata_full` is valid from **2019-10-01** (the
    30-09-2019 file holds 27-Jun-2019 rows). NSE has no `exchdisstime` and
    only minute-precision `an_dt` before ~Aug 2020.
  - **Bug found and fixed (schema v3):** BSE past days have no
    `TotalPageCnt`, so 0.1.2 marked them complete after one page. Page count
    now from ROWCNT; completeness needs n_records ≥ ROWCNT. Verified with a
    real backfill of 2023-10-03 (996/996 rows).
  - T1.3 done. NSE `an_dt` mapped to `exch_submitted_ts` (receipt time):
    `difference == exchdisstime − an_dt` exactly on 2,410/2,410 real rows.
    Parser `nse-ann-v2` warns if that identity breaks. `reparse nse_ann` ran
    (0 docs: the recorder has not run on this machine yet).
  - T1.2 done. All five probes pass live (BSE 272 rows/6 pages, NSE 110 rows,
    EOD 3,534 rows for 01-Oct, bands 3,574, instruments 2,593). Trimmed
    samples in `tests/fixtures/real/` (byte-exact via `.gitattributes`), 13
    tests. Finding: `sec_list.csv` remarks carry GSM stages (useful for T2.9).
  - T1.1 done. Environment: Windows 11 Home (10.0.26200), Python 3.14.2
    (CI tests 3.11/3.12), fresh `.venv`. `data/` did not exist; `gats init`
    created it (empty DB, schema v2). ruff, mypy strict and 103 tests green;
    `gats version` = 0.1.2.
  - Six tracked files (CLAUDE.md, pyproject.toml, README.md, CHANGELOG.md,
    .gitignore, .env.example) were deleted in the working tree, apparently by
    accident; restored from HEAD.
  - Decision: the user asked for autonomous end-to-end work, so HUMAN steps
    and gates are queued under "Waiting on the human" and work continues on
    every task whose dependencies are met, instead of stopping.
- **2026-09-28:**
  - 0.1.2 built in a Claude.ai session and handed to Claude Code, with
    CLAUDE.md, the roadmap and this tracker.
  - Live findings so far:
    - BSE throttles bursts.
    - NSE live lag is ~150 s.
    - The user's laptop is not always on, and the reconcile job heals gaps.
