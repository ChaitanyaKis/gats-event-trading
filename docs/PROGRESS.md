# Progress

Claude Code updates this after every task. Newest log entry first.

## Status

- **Version:** 0.2.0 · **Schema:** v7 · **Milestone:** M3 (M1 waits only on T1.6, HUMAN)
- **Next up:** M5 T5.3 Reaction curves: pre-registration + code (the real run needs bars). Then M6 (T6.1 cost model onwards). Waiting on the human: M3 run (3), labels (5), Upstox token (6)

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
   T4.6 (rules vs LLM vs cascade).

6. **T5.1 Upstox Analytics Token** (5 minutes; needs an Upstox account).
   It is read-only (it cannot trade), valid for a year, and needs no daily
   login. Open https://account.upstox.com/developer/apps#analytics, go to
   the **Analytics** tab, click **Generate Token**, confirm, and copy the
   whole token with the clipboard icon. Add one line to `C:\Projects\GATS\.env`
   (Claude never reads that file):
   ```
   GATS_UPSTOX_ANALYTICS_TOKEN=<paste the token>
   ```
   Then type `/gats`. Claude probes the token, checks a day of bars against
   NSE's own end-of-day file, checks that a stock whose ISIN changed still has
   its older bars, and then gives you one resumable command for the full
   event-window fetch (`gats bars events`, about 1–2 h at Upstox's rate limit).

## Blocked

(none)

## Milestones

- [x] M1 core recorder (0.1.0 → 0.1.2)
- [ ] M1 finishing: T1.1 ✅ · T1.2 ✅ · T1.3 ✅ · T1.4 ✅ · T1.5 ✅ · T1.6
- [x] M2 Reference data & entity resolution (0.2.0): T2.1 ✅ · T2.2 ✅ · T2.3 ✅ · T2.4 ✅ · T2.5 ✅ · T2.6 ✅ · T2.7 ✅ · T2.8 ✅ · T2.9 ✅
- [ ] M3 Event study (G1 kill test): T3.0 ⏳ (human) · T3.1 ⚠️ · T3.2 ✅ · T3.3 ✅ · T3.4 ✅ (code; real run ⏳ backfill) · T3.5 ⏳ (gate, after the real run)
- [ ] M4 LLM extraction (infrastructure for ORDER_WIN; scope confirmed at G1): T4.1 ✅ · T4.2 ✅ · T4.3 ✅ · T4.4 ✅ · T4.5 ⏳ (code ✅; labels: human) · T4.6 · T4.7
- [ ] M5 Intraday data & reaction curves (G1b): T5.1 ⏳ (code ✅; token: human, item 6) · T5.2 ⏳ (code ✅; the fetch needs the token) · T5.3 · T5.4
- [ ] M6 Backtester (G2)
- [ ] M7 Paper trading (G3)
- [ ] M8 Live pilot (G4, human-only)

## Tasks done

- M1: recorder, point-in-time schema, raw store, probe/backfill/reparse
  (0.1.0)
- M1: BSE one-day queries, attachment storage policy (0.1.1)
- M1: BSE throttle retries, `backfill_days` (schema v2), reconcile job,
  deeper catch-up, `inspect-bad` (0.1.2)

## Log

- **2026-10-03:**
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
