# Progress

Claude Code updates this after every task. Newest log entry first.

## Status

- **Version:** 0.1.2 · **Schema:** v3 · **Milestone:** M1 (finishing)
- **Next up:** T1.5 Status UX

## Waiting on the human (HUMAN)

- Keep the recorder running in its own terminal (PC awake, sleep disabled):
  `powershell -ExecutionPolicy Bypass -File scripts\run_recorder.ps1`

## Blocked

(none)

## Milestones

- [x] M1 core recorder (0.1.0 → 0.1.2)
- [ ] M1 finishing: T1.1 ✅ · T1.2 ✅ · T1.3 ✅ · T1.4 ✅ · T1.5 · T1.6
- [ ] M2 Reference data & entity resolution
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
