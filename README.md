# GATS: Data Recorder (Milestone M1)

GATS is an event-driven research and trading platform for Indian equities.
This repository currently ships **M1: the 24/7 data recorder**, the
foundation every later milestone builds on. It records, point-in-time:

| Data | Source | Schedule | Why it matters |
|---|---|---|---|
| Corporate announcements | BSE (primary), NSE | every 30 s (BSE), 120 s (NSE); slower at night | the event stream the strategy trades on |
| Filing attachments (PDFs) | BSE / NSE | every 60 s, 20 per batch; material categories only, ≤ 5 MB | the full text the LLM will read |
| End-of-day prices + delivery % | NSE `sec_bhavdata_full` | after 18:00 IST, with 10-day catch-up | returns for event studies |
| Price bands (circuit limits) | NSE `sec_list.csv` | daily after 08:00 IST | can we even trade it? |
| Instrument list (symbol ↔ ISIN) | NSE `EQUITY_L.csv` | daily after 08:00 IST | entity resolution across exchanges |

Price bands and the instrument list exist only as "today's file", so their
history starts the day you start recording. **Start the recorder early.**

**Attachment storage policy.** Downloading every filing PDF costs gigabytes a
day (annual reports, investor presentations). By default only material
categories (orders, results, ratings, M&A, fund raising, ...) are downloaded,
up to 5 MB each; noise such as trading-window notices is skipped. The URL of
*every* attachment is stored regardless, so anything skipped can be fetched
later. Tune with `GATS_ATTACHMENTS_*` in `.env`.

---

## ⚠️ First run: verify the endpoints

The exchange URLs and field names were written from knowledge of the
exchanges' public websites but **could not be tested from the build
environment** (no network access to NSE/BSE). The code is built for this:

- every raw payload is stored before parsing (`data/raw/`),
- `gats probe` shows exactly how the parser handles a real response,
- `gats reparse` rebuilds the tables after a parser fix, without refetching.

Before running the recorder, run each probe and check the output:

```powershell
gats probe bse
gats probe nse
gats probe eod
gats probe bands
gats probe instruments
```

For each one, check that the status is 200, `records` > 0, the field coverage
is mostly full, and `unknown_fields` doesn't hide something important. If a
probe fails, share the output and the saved sample in `data/probes/`: the
parser gets fixed against the real payload. If a URL has moved, override it
in `.env` (see `.env.example`); no code change is needed.

---

## Quick start (Windows, PowerShell)

Requires Python 3.11+ ([python.org](https://www.python.org/downloads/); tick "Add to PATH").

```powershell
cd gats
py -m venv .venv
.venv\Scripts\Activate.ps1          # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
python -m pip install --upgrade pip
pip install -e ".[dev]"

gats init                            # creates data\ and data\gats.db
gats probe bse                       # ...and the other probes above
gats record                          # runs until Ctrl+C
gats status                          # in a second terminal
```

### Try it offline first (no exchange access needed)

```powershell
python scripts\mock_exchange.py      # terminal 1: fake exchange (add --flaky 3 to simulate throttling)
copy scripts\smoke.env .env          # terminal 2: point GATS at it
gats record                          # watch data arrive; Ctrl+C to stop
gats status
del .env                             # back to real endpoints
```

---

## Commands

| Command | What it does |
|---|---|
| `gats init` | Create the data directory and database |
| `gats probe {bse,nse,eod,bands,instruments} [--date YYYY-MM-DD]` | Fetch one sample, save it, report parser coverage |
| `gats record` | Run all recorder jobs until stopped |
| `gats status [--json]` | Problems in plain words first (recorder stopped, job failing > 30 min, BSE throttling, disk, reconcile backlog), then counts, latency, last fetch per job, heartbeat |
| `gats doctor [--network]` | Health checks with a fix for each problem; `--network` makes one request per source. Exit code 1 on failure |
| `gats backfill eod --start D --end D` | Load historical daily prices (resumable) |
| `gats backfill announcements --source bse\|nse --start D --end D` | Load historical filings day by day (resumable) |
| `gats reparse {bse_ann,nse_ann,nse_eod,nse_bands,nse_instruments}` | Re-run the current parser over stored raw payloads |
| `gats inspect-bad [--limit N]` | Recent failed fetches and the payloads that failed to parse |
| `gats version` | Installed version (check it after every update) |

**Backfill pacing:** BSE is limited to one request every 2 seconds
(`GATS_HOST_MIN_INTERVAL_S`), and it returns 50 filings per request, so a
year of history takes several hours. Run it overnight, and rerun the same
command to resume. A day counts as done only when *every* page was fetched.
BSE sometimes throttles with `{}` or an HTML block page; those pages are
retried, and partial days are retried on the next run.

**Gaps heal automatically.** The `reconcile` job re-collects each of the last
7 days in full until it is complete, so a PC that was off, throttling, or a
burst of filings between polls leaves no permanent hole. For gaps older than
7 days, run `gats backfill announcements` for that range.

---

## Data rules (point-in-time)

- Every timestamp is stored as **UTC**. Exchange times (IST) are converted on
  parse, and naive datetimes are rejected at the database layer.
- `event_ts` is when the event happened (exchange dissemination time).
  `available_at` is when **this system** first knew it.
- `ingest_mode` is `live`, `catchup` (first poll after a restart) or
  `backfill` (historical). Live-latency statistics use `live` rows only.
- Research code reads through `gats.pit.AsOf`, which only returns rows with
  `available_at <= as_of`. This is the structural guard against look-ahead bias.
- The raw store (`data/raw/`) is content-addressed and immutable. Never delete it.

---

## Running it 24/7

- **Windows PC:** `scripts\run_recorder.ps1` restarts the recorder if it
  exits. Disable sleep in Power settings, because a sleeping PC records
  nothing. Copies of the data are only as safe as the disk, so back up `data\`.
- **Linux VM (recommended, e.g. Oracle Always Free):** see
  `deploy/gats-recorder.service` for a systemd unit with automatic restarts.
  Switch to Postgres with `pip install -e ".[postgres]"` and `GATS_DB_URL`.

## Be a polite client

Defaults are conservative: one request per second per host, exponential
backoff, and `Retry-After` honoured. Keep it that way. Aggressive polling
gets IPs blocked and may breach the sites' terms of use. Review the NSE and
BSE terms and use the data for personal research.

---

## Development

```powershell
pytest              # 103 tests, no network needed
ruff check src tests scripts
mypy                # strict
```

CI (`.github/workflows/ci.yml`) runs the same on Ubuntu **and Windows**.

```
src/gats/
  config.py        settings (.env / GATS_* env vars)
  timeutil.py      IST↔UTC, parsing
  net.py           polite HTTP client (throttle, retries, cookie warm-up)
  rawstore.py      content-addressed raw payload store
  db/              schema (bitemporal), engine, repository
  sources/         pure parsers: bse, nse, nse_archives
  ingest.py        fetch → store raw → parse → write (shared by all commands)
  recorder.py      24/7 job scheduler + heartbeat
  pit.py           point-in-time reader for research
  status.py, cli.py
scripts/           mock exchange, smoke settings, Windows runner
deploy/            systemd unit
```

## Building with Claude Code

The rest of the project (M2 → M8) is built by Claude Code following
`CLAUDE.md`, `docs/ROADMAP.md` and `docs/PROGRESS.md`. Setup and the
one-time prompt are in [`docs/KICKOFF.md`](docs/KICKOFF.md). After that,
type `/gats` in each session.

## Roadmap

M1 recorder (done) → M2 reference data + entity resolution → M3 daily event
study (the cheap test of whether an edge exists) → M4 LLM extraction →
M5 minute data + reaction curves → M6 backtester → M7 paper trading →
M8 live pilot (human-activated only). Details: [`docs/ROADMAP.md`](docs/ROADMAP.md);
rationale: [`docs/DESIGN.md`](docs/DESIGN.md).
