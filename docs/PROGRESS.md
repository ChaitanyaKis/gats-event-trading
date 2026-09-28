# Progress

Claude Code updates this after every task. Newest log entry first.

## Status

- **Version:** 0.1.2 · **Schema:** v2 · **Milestone:** M1 (finishing)
- **Next up:** T1.1 Bootstrap & health check

## Waiting on the human (HUMAN)

- Keep the recorder running in its own terminal (PC awake, sleep disabled):
  `powershell -ExecutionPolicy Bypass -File scripts\run_recorder.ps1`

## Blocked

(none)

## Milestones

- [x] M1 core recorder (0.1.0 → 0.1.2)
- [ ] M1 finishing: T1.1 · T1.2 · T1.3 · T1.4 · T1.5 · T1.6
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

- **2026-09-28:**
  - 0.1.2 built in a Claude.ai session and handed to Claude Code, with
    CLAUDE.md, the roadmap and this tracker.
  - Live findings so far:
    - BSE throttles bursts.
    - NSE live lag is ~150 s.
    - The user's laptop is not always on, and the reconcile job heals gaps.
