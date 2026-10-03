# Progress

Claude Code updates this after every task. Newest log entry first.

## Status

- **Version:** 0.2.0 · **Schema:** v5 · **Milestone:** M3 (M1 waits only on T1.6, HUMAN)
- **Next up:** T3.2 Pre-registration (T3.0 plan is waiting for the human: item 3)

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

## Blocked

(none)

## Milestones

- [x] M1 core recorder (0.1.0 → 0.1.2)
- [ ] M1 finishing: T1.1 ✅ · T1.2 ✅ · T1.3 ✅ · T1.4 ✅ · T1.5 ✅ · T1.6
- [x] M2 Reference data & entity resolution (0.2.0): T2.1 ✅ · T2.2 ✅ · T2.3 ✅ · T2.4 ✅ · T2.5 ✅ · T2.6 ✅ · T2.7 ✅ · T2.8 ✅ · T2.9 ✅
- [ ] M3 Event study (G1 kill test): T3.0 ⏳ (human) · T3.1 ⚠️ ·  T3.2 · T3.3 · T3.4 · T3.5
- [ ] M4 LLM extraction
- [ ] M5 Intraday data & reaction curves (G1b)
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
