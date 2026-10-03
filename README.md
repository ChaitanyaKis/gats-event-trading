# GATS

GATS is an event-driven research and trading platform for Indian equities.
It currently ships **M1, the 24/7 data recorder**, and **M2, reference data**
(which company each filing is about, across NSE and BSE, point-in-time).
It records, point-in-time:

| Data | Source | Schedule | Why it matters |
|---|---|---|---|
| Corporate announcements | BSE (primary), NSE | every 30 s (BSE), 120 s (NSE); slower at night | the event stream the strategy trades on |
| Filing attachments (PDFs) | BSE / NSE | every 60 s, 20 per batch; material categories only, ≤ 5 MB | the full text the LLM will read |
| End-of-day prices + delivery % | NSE `sec_bhavdata_full` | after 18:00 IST, with 10-day catch-up | returns for event studies |
| Price bands (circuit limits) | NSE `sec_list.csv` | daily after 08:00 IST | can we even trade it? |
| Instrument list (symbol ↔ ISIN) | NSE `EQUITY_L.csv` | daily after 08:00 IST | entity resolution across exchanges |
| Index closes (Nifty 50/500, sectors, …) | NSE `ind_close_all_DDMMYYYY.csv` | after 18:00 IST | benchmarks for abnormal returns |
| BSE scrip master (scrip code ↔ ISIN) | BSE "List of Scrips" API | daily after 08:00 IST | ties BSE filings to securities |
| Symbol changes, holidays | NSE `symbolchange.csv`, holiday API | daily | joins across renames; trading calendar |
| Corporate actions | NSE corporate-actions API | daily, −30…+90 days | split/bonus-safe returns |
| ASM / GSM surveillance lists | NSE report APIs | daily | what must never be traded |

Derived every day: a **security master** (NSE rename chains and BSE scrip
codes joined through ISINs), each filing's **security link**, and
**cross-exchange events** (the same disclosure on BSE and NSE grouped, timed
at the earliest dissemination).

Price bands and the instrument list exist only as "today's file", so their
history starts the day you start recording. **Start the recorder early.**

**Attachment storage policy.** Downloading every filing PDF costs gigabytes a
day (annual reports, investor presentations). By default only material
categories (orders, results, ratings, M&A, fund raising, ...) are downloaded,
up to 5 MB each; noise such as trading-window notices is skipped. The URL of
*every* attachment is stored regardless, so anything skipped can be fetched
later. Tune with `GATS_ATTACHMENTS_*` in `.env`.

---

## Verified endpoints

Every source above was probed against the live sites; the findings (formats,
quirks such as NSE serving the previous day's file on a holiday, or reporting
today's symbol for old filings) are in [`docs/DATA_SOURCES.md`](docs/DATA_SOURCES.md),
with trimmed real samples under `tests/fixtures/real/`. To re-check:

```powershell
gats doctor --network        # one request per source, parsed
gats probe bse               # ... nse | eod | indices | bands | instruments
```

If a probe fails, the payload is saved in `data/probes/` and the raw store;
fix the parser, then `gats reparse <kind>`. A moved URL can be overridden in
`.env` (see `.env.example`) without code changes.

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
| `gats probe {bse,nse,eod,indices,bands,instruments} [--date YYYY-MM-DD]` | Fetch one sample, save it, report parser coverage |
| `gats record` | Run all recorder jobs until stopped |
| `gats status [--json]` | Problems in plain words first (recorder stopped, job failing > 30 min, BSE throttling, disk, reconcile backlog), then counts, latency, last fetch per job, heartbeat |
| `gats doctor [--network]` | Health checks with a fix for each problem; `--network` makes one request per source. Exit code 1 on failure |
| `gats backfill eod --start D --end D [--no-weekends]` | Load historical daily prices (resumable; weekends included for special sessions) |
| `gats backfill indices --start D --end D` | Load historical index closes (resumable) |
| `gats backfill corporate-actions --start D --end D` | Load NSE corporate actions, a year per request |
| `gats backfill announcements --source bse\|nse --start D --end D` | Load historical filings day by day (resumable) |
| `gats reparse <kind>` | Re-run the current parser over stored raw payloads (`gats reparse --help` lists kinds) |
| `gats refdata update` | Take today's snapshot of every reference file now (the recorder does it daily) |
| `gats refdata build` | Rebuild the security master and link filings to it |
| `gats refdata dedupe [--start D --end D]` | Group the same disclosure on BSE and NSE into one event |
| `gats refdata coverage [--days N]` | Share of recent filings linked to a security; lists what is not |
| `gats refdata resolve nse_symbol ZOMATO --date 2024-08-01` | What an identifier meant on a date |
| `gats events classify` / `gats events coverage` | Type filings with `configs/event_taxonomy.yaml`; report what stays OTHER |
| `gats research event-study` | Run the pre-registered M3 study (refuses an edited config or incomplete data) |
| `gats extract fetch --type ORDER_WIN` / `gats extract texts` / `gats extract coverage` | Download an event type's attachments (whatever the storage policy) and turn them into text |
| `gats extract run --type ORDER_WIN --mode cascade [--limit N]` | Extract order value, counterparty, duration... (`rules`, `llm` or `cascade`; LLM modes need Ollama running) |
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
pytest              # ~285 tests, no network needed
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
  sources/         pure parsers, one module per exchange file or API
  ingest.py        fetch → store raw → parse → write (shared by all commands)
  refdata/         security master, symbol history, calendar, corporate
                   actions, dedupe, surveillance, versioned snapshots
  recorder.py      24/7 job scheduler + heartbeat
  pit.py           point-in-time reader for research (AsOf)
  health.py, status.py, cli.py
scripts/           mock exchange, smoke settings, Windows runner
deploy/            systemd unit
```

## Building with Claude Code

The rest of the project (M2 → M8) is built by Claude Code following
`CLAUDE.md`, `docs/ROADMAP.md` and `docs/PROGRESS.md`. Setup and the
one-time prompt are in [`docs/KICKOFF.md`](docs/KICKOFF.md). After that,
type `/gats` in each session.

## Roadmap

M1 recorder (done) → M2 reference data + entity resolution (done) → M3 daily event
study (the cheap test of whether an edge exists) → M4 LLM extraction →
M5 minute data + reaction curves → M6 backtester → M7 paper trading →
M8 live pilot (human-activated only). Details: [`docs/ROADMAP.md`](docs/ROADMAP.md);
rationale: [`docs/DESIGN.md`](docs/DESIGN.md).
