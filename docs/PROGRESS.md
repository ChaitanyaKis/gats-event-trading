# Progress

Claude Code updates this after every task. Newest log entry first.

## Status

- **Version:** 0.1.2 · **Schema:** v4 · **Milestone:** M2 (M1 waits only on T1.6, HUMAN)
- **Next up:** T2.3 Security master

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

## Blocked

(none)

## Milestones

- [x] M1 core recorder (0.1.0 → 0.1.2)
- [ ] M1 finishing: T1.1 ✅ · T1.2 ✅ · T1.3 ✅ · T1.4 ✅ · T1.5 ✅ · T1.6
- [ ] M2 Reference data & entity resolution: T2.1 ✅ · T2.2 ✅ · T2.3 · T2.4 · T2.5 · T2.6 · T2.7 · T2.8 · T2.9
- [ ] M3 Event study (G1 kill test)
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
