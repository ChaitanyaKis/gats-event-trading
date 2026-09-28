# CLAUDE.md — GATS

GATS is an event-driven research and trading platform for Indian equities
(NSE/BSE). It's built by Chaitanya, a B.Tech student learning to be a
world-class AI and software engineer. **You (Claude Code) build it on your
own, task by task**, following `docs/ROADMAP.md` and recording state in
`docs/PROGRESS.md` so any session can resume where the last one stopped.

**Read at session start, in order:** this file → `docs/PROGRESS.md` → the
current milestone in `docs/ROADMAP.md`. Also read `docs/DESIGN.md` before
starting a milestone, and `docs/DATA_SOURCES.md` before touching any external
source.

---

## Work Loop

Run it when the user types `/gats`, "continue", "go" or "next".

1. **Health check.**
   - If `.venv` is missing: `py -m venv .venv`, then
     `.venv/Scripts/python -m pip install -e ".[dev]"`.
   - If `data/gats.db` is missing: `.venv/Scripts/gats init`.
   - Run the full test suite. It must be green before new work starts; if it
     isn't, fixing it *is* the task.
   - Recorder: read `data/heartbeat.json`. If it is missing, or any job's
     `last_ok_at` is older than 15 min, ask the user to start it (see
     Commands) and keep working.
2. **Pick a task.** Take PROGRESS.md "Next up". If that task is blocked, take
   the first unchecked ROADMAP task whose dependencies are done and that isn't
   waiting on a HUMAN step.
3. **Plan** in 3–7 bullets in your reply. Wait for approval only on tasks
   tagged `[ASK]`.
4. **Implement** in small steps. Write tests with the code. Reuse existing
   modules; don't rewrite what works.
5. **Verify.**
   - ruff, mypy (strict) and pytest are all green.
   - Data tasks: run the real command on a small range and inspect the output.
   - Research tasks: open the generated report and sanity-check the numbers.
6. **Commit** in small commits.
   - Message format: `T<id>: <summary>`, with a body explaining *why*.
   - Add attribution trailers only in Claude Code's default form. Don't copy
     trailers from older commits.
7. **Record.**
   - Tick the task in PROGRESS.md, add a 1–3 line log entry (newest first),
     and set "Next up".
   - Add user-visible changes to CHANGELOG.md.
   - At the end of each milestone: bump the minor version and write
     `docs/learning/M<n>.md` (see "Communicating").
8. **Continue** to the next task without waiting. **Stop only when:**
   - you reach a `HUMAN` step or a decision gate (`G*`), or
   - you've made 3 honest attempts and are still blocked (write the evidence
     under "Blocked" in PROGRESS.md), or
   - the user interrupts.

   When you stop, finish with **what changed / blockers / exact
   copy-pasteable commands the user must run**.

Never tell the user work is done unless tests pass and the change is
committed.

---

## Commands (Windows, from repo root)

The shell may be PowerShell or Git Bash, so always call the venv's executables
directly:

```
.venv/Scripts/python -m pytest -q
.venv/Scripts/python -m ruff check src tests scripts
.venv/Scripts/python -m ruff format src tests scripts
.venv/Scripts/python -m mypy
.venv/Scripts/gats status | inspect-bad | probe <target> | backfill ... | reparse <kind> | version
```

- **The recorder runs in the user's own terminal:**
  `powershell -ExecutionPolicy Bypass -File scripts\run_recorder.ps1`.
  Never run `gats record` yourself; it runs forever and would block you.
- **Long commands** (over ~10 min, e.g. multi-year backfills) are HUMAN steps.
  Give the exact command to run in a separate terminal, then continue with
  independent tasks. Backfills are resumable and idempotent.
- **Offline end-to-end check:** `scripts/mock_exchange.py` with
  `scripts/smoke.env` (see README).

---

## Architecture (follow it, don't reinvent it)

- `src/gats/`, Python 3.11+, SQLAlchemy **Core** (not ORM), pydantic v2,
  typer CLI, httpx.
  - Database: SQLite by default, Postgres optional.
  - Heavy time series (minute bars, research frames) go in Parquet, queried
    with DuckDB.
- Module map:
  - `config.py`: Settings, `GATS_*` env vars.
  - `timeutil.py`: UTC storage, fixed IST offset.
  - `net.py`: `PoliteClient`; all HTTP goes through it.
  - `rawstore.py`: immutable content-addressed payload store.
  - `db/`: `schema.py`, `engine.py` (versioned, additive migrations), `repo.py`.
  - `sources/`: pure parsers, versioned by `PARSER_VERSION`.
  - `ingest.py`: fetch → store raw → parse → write.
  - `recorder.py`: 24/7 jobs.
  - `pit.py`: the `AsOf` point-in-time reader.
  - `cli.py`, `status.py`.
- New subsystems get their own packages: `refdata/`, `research/`,
  `extract/`, `marketdata/`, `backtest/`, `risk/`, `oms/`, `broker/`,
  `runtime/`.
- **The same strategy code runs in backtest, paper and live.** Only the data
  feed and broker implementations change.
- **The LLM extracts facts.** Deterministic code decides risk and orders.

## Engineering rules (non-negotiable)

1. **Time.** Store UTC-aware datetimes only; naive datetimes raise. Exchange
   times are IST, converted in parsers.
2. **Point-in-time.**
   - Every row has `available_at`.
   - Research and strategy code reads only through `gats.pit.AsOf` (extend it
     as needed).
   - Add a leak test for every new feature.
3. **Raw first.**
   - Every external payload is stored in the raw store before parsing.
   - Parsers are pure functions with a `PARSER_VERSION`; fix them, then
     `reparse`.
   - Never delete or modify anything under `data/`.
4. **Idempotent writes.** Use natural keys and insert-ignore/upsert. Every
   job must be safely re-runnable after a crash.
5. **Schema changes** are additive. Bump `SCHEMA_VERSION`, register the
   upgrade in `db/engine.py`, and test that the previous version upgrades in
   place.
6. **HTTP.**
   - Use `PoliteClient` only. Never lower `min_request_interval_s` or the
     per-host intervals, and never remove backoff.
   - Only one BSE-heavy process at a time. If the recorder is running, give
     backfills `GATS_HOST_MIN_INTERVAL_S='{"api.bseindia.com": 4}'`.
7. **Tests.**
   - No network in tests: mock with respx and use saved real samples in
     `tests/fixtures/real/`.
   - Never weaken, skip or xfail a test to get green; fix the cause.
8. **Quality.** Keep ruff clean and mypy strict clean. Type every function.
   Docstrings explain *why*, not *what*.
9. **Dependencies.** Prefer the stdlib and what's already installed. Justify
   each new dependency in the commit body and add it to `pyproject.toml`
   with a lower bound.
10. **Config over code.** Endpoints, thresholds, costs and schedules live in
    `Settings` or versioned YAML under `configs/`, documented in
    `.env.example`.
11. **Portability.** Code must work on Windows and Linux (CI runs both). Use
    `pathlib`, and no shell-specific tricks in Python.

## Verify, never invent

- **External APIs and files.** Never assume an endpoint, field or format.
  Probe it (extend `gats probe`), save a trimmed real sample to
  `tests/fixtures/real/`, and record it in `docs/DATA_SOURCES.md` with the
  verification date. If it can't be verified, stop and mark it
  `UNVERIFIED — HUMAN` in PROGRESS.md.
- **Fees, taxes and regulations.** Take values from official sources (SEBI,
  NSE, BSE, the broker, the Income Tax Department). Store the URL and the
  date checked next to each value. Never use remembered numbers.
- **Libraries, papers and benchmarks.** Cite only what exists. If you're
  unsure, say so.

## Research integrity (this is what makes results trustworthy)

- **Pre-register before computing any returns.** Write the hypotheses,
  windows, entry/exit rules, costs, filters, train/test split dates, pass
  criteria and multiple-testing correction in
  `docs/research/<study>_prereg.md`, and commit it first.
- **The holdout is used once.** No tuning after seeing test results. A new
  idea needs a new pre-registration, and it counts as a new trial.
- Log every experiment (the registry arrives in M6; before that, use
  `docs/research/trials.md`). The trial count feeds the deflated-Sharpe and
  Benjamini–Hochberg corrections.
- **Report failures as clearly as successes.** "No edge found" is a valid,
  valuable result. When a decision gate fails, stop and let the human decide.

## Safety (hard rules; the settings deny list enforces some of them)

- **Never place, modify or cancel a real order**, and never enable live
  trading.
- Never run `gats gate approve` or `gats live`; they're human-only.
- The live runtime must refuse to start without a human-created approval
  record. Never add a bypass.
- **Secrets** (broker keys, tokens, Telegram bot token) live in `.env`,
  which you must not read or edit. Tell the user which keys to add; code reads
  them through `Settings`. Never print, log or commit secrets.
- Never commit `data/`, `.env` or large files. Never force-push. Ask before
  `git push`.
- Money, accounts, sign-ups, credentials and anything irreversible are HUMAN
  steps.

## Communicating with Chaitanya

- **Be concise.** When you stop, give what changed, any blockers, and the next
  action. No filler.
- **Explain design decisions** in one or two sentences of *why*. He's here to
  learn how production systems are built. Challenge bad ideas, including his.
- **After each milestone**, write `docs/learning/M<n>.md` (one page max):
  - what was built and why this design,
  - the key concepts (e.g. look-ahead bias, idempotency),
  - what could go wrong, and
  - 2–3 real resources to read (only ones you're sure exist).
- **HUMAN requests** must be exact: numbered steps with copy-pasteable
  PowerShell commands, and what to send back.
