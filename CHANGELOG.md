# Changelog

## Unreleased
Schema v6 (new tables only), upgraded automatically.

- **Order facts:** `gats extract run --type ORDER_WIN --mode rules|llm|cascade`
  extracts each order win's value, counterparty, domestic/export, duration
  and repeat flag into `extractions`. Rules read SEBI's disclosure annexure
  or the covering letter, including amounts in words ("Rupees ... Crore ...
  only"), figures split by the PDF and ranges (lower bound). A local LLM
  (Ollama; `GATS_OLLAMA_URL`, `GATS_LLM_MODEL`) answers when the rules are
  unsure. It only copies text: code computes every number and rejects an
  amount the filing does not contain. Replies are cached per document,
  prompt and model (`llm_extractions`).
- **Attachment text:** downloaded attachments are converted to text
  (`document_texts`, recorder job `extract`; scanned PDFs flagged
  `needs_ocr`). `gats extract fetch --type ORDER_WIN` downloads an event
  type's attachments regardless of the storage policy; `gats extract texts`
  and `gats extract coverage` report results. New dependency: pypdf.
- **M3 report:** `gats research event-study` also writes
  `reports/M3_event_study.md` (G1 verdict from the pre-registered rule,
  train vs test for every type, liquidity buckets, CAR plot, filter
  accounting, spot-check list) and logs the trial; `gats research report
  --run <id>` rebuilds it from a saved run.
- **Event-study engine:** `gats research event-study` runs the
  pre-registered M3 study (`configs/studies/m3_event_study.yaml`) and writes
  a per-event Parquet frame. It refuses an edited config or incomplete data.
  New optional extra `research` (numpy, pyarrow, matplotlib).
- **Event types:** every filing is typed by a versioned, rule-based
  taxonomy (`configs/event_taxonomy.yaml`): order wins, results, ratings,
  buybacks, bonus/splits, dividends, M&A, fund raises, pledges, management
  changes, litigation, insolvency, business updates, and procedural noise.
  New recorder job `classify`; `gats events classify` and
  `gats events coverage`. New dependency: PyYAML.

## 0.2.0 (2026-10-03)
M1 finishing (verified endpoints, real fixtures, health checks, two
recorder bug fixes) and M2 (reference data). Schema v5, upgraded
automatically; run `gats refdata update`, `gats refdata build` and
`gats refdata dedupe` once after upgrading.

- **Surveillance lists:** NSE's ASM (long and short term) and GSM lists are
  recorded daily (`nse_surveillance` job) as dated history;
  `AsOf.surveillance(symbol)` reports list stages, the GSM remark and
  trade-for-trade status (BE/BZ/ST/SZ series).
- **Corporate actions:** NSE's corporate actions are recorded daily
  (`nse_corp_actions` job; `gats backfill corporate-actions` for history),
  with splits, bonuses and consolidations turned into share multipliers.
  Daily returns must be computed with `ReturnAdjuster` (`AsOf.return_adjuster()`):
  NSE's `PREV_CLOSE` is not adjusted on ex-dates.
- **Benchmark indices:** daily closes of every NSE index (Nifty 50/500,
  Midcap 150, Smallcap 250, sectors, …) via a new recorder job
  `nse_indices`, `gats backfill indices --start … --end …` and
  `gats probe indices`.
- **Trading calendar:** `gats.refdata.calendar` knows NSE sessions from the
  EOD files, NSE's holiday list (new daily `nse_holidays` job) and weekends,
  flags special sessions, and gives `next_session_open(t)`,
  `session_open/close(d)` and `shift(d, n)` in IST hours 09:15–15:30
  (`GATS_SESSION_OPEN_IST`, `GATS_SESSION_CLOSE_IST`).
- **Fix: EOD on holidays.** NSE serves a copy of the previous session's file
  under a weekday holiday's name instead of a 404, so the recorder refetched
  holiday files every 30 minutes for 10 days. Each day's outcome is now kept
  in `eod_days` (`loaded` / `not_published` / `other_day`) and retries stop.
  Weekends are checked too, since special sessions (e.g. the Sunday budget
  session of 2026-02-01) publish files; `gats backfill eod --no-weekends`
  skips them.
- **Cross-exchange events:** the same disclosure filed on BSE and NSE is
  grouped into one event timed at the earliest dissemination (recorder job
  `dedupe`, `gats refdata dedupe` for history). Research reads events via
  `AsOf.events_since()`.- Announcements store the attachment size each exchange reports
  (`attachment_size`, schema v5; parsers `bse-ann-v2`, `nse-ann-v3`). Run
  `gats reparse bse_ann` and `gats reparse nse_ann` to fill older rows.
- Fix: `gats reparse bse_ann|nse_ann` skipped rows whose payload had first
  been stored by `gats probe` (identical bytes share one raw document).
- **Filings linked to securities:** every announcement gets a
  `security_id` (`announcement_security`), kept current by new recorder jobs
  `master_build` (daily) and `link` (every minute). `gats refdata build` also
  links; `gats refdata coverage` reports the linked share per exchange and
  lists what is unresolved.
- **Security master:** `gats refdata build` joins NSE symbols (with their
  rename history) and BSE scrip codes through ISINs into stable
  `security_id`s with dated identifier windows; `gats refdata resolve
  nse_symbol ZOMATO --date 2024-08-01` shows what an identifier meant on a
  date. `gats refdata update` also snapshots NSE instruments and bands.
- **NSE symbol history:** a daily `nse_symbol_changes` job stores NSE's
  symbol-change file (every rename since 1999); `gats.refdata.symbols`
  translates a symbol between dates (e.g. ETERNAL in 2024 → ZOMATO).
  `gats refdata update` now also fetches it.
- **BSE scrip master:** a daily `bse_scrips` recorder job snapshots BSE's
  full scrip list (scrip code → ISIN, ticker, group, status, incl. delisted)
  into versioned rows. `gats refdata update` takes a snapshot now;
  `gats refdata coverage` shows how many recent filings resolve to an ISIN.
  Schema v4 (automatic upgrade).- `gats status` starts with a health section: each problem in plain words
  with a fix. New `gats doctor [--network]` runs the same checks plus
  environment checks (and one request per source), exiting 1 on failure.
  Thresholds: `GATS_STATUS_*` (see `.env.example`).
- **Fix: BSE past days were marked complete after one page.** BSE omits
  `TotalPageCnt` for past days; the page count now comes from `ROWCNT`, and a
  day is complete only when at least `ROWCNT` rows were collected. Schema v3
  (upgraded automatically) adds `backfill_days.expected_records` and reopens
  BSE days the bug had closed, so the reconcile job refetches them.- NSE announcements now store the exchange receipt time (`an_dt`) as
  `exch_submitted_ts` (parser `nse-ann-v2`); run `gats reparse nse_ann` to
  update rows recorded earlier.- Tests run against trimmed real exchange payloads (`tests/fixtures/real/`),
  made with `scripts/trim_fixture.py`.

## 0.1.2 (2026-09-28)
Fixes from the first days of live running.
- **BSE throttling:** a page that should hold rows but returns `{}` or an HTML
  block page is retried (15 s, then 30 s). BSE requests are limited to one
  every 2 s. The homepage is loaded once for session cookies.
- **Backfill correctness:** a day is marked done only when every page was
  fetched (`backfill_days` table, schema v2, upgraded automatically). Before,
  a day that failed on page 3 was skipped forever on rerun.
- **Automatic gap healing:** a new `reconcile` job re-collects the last 7 days
  until each is complete. Days that keep failing are given up after 3
  answered attempts; network outages don't count toward that.
- **Deeper catch-up:** the first BSE poll after a start pages up to 100 pages
  (was 10), so a restart mid-day doesn't miss the morning's filings.
- `gats inspect-bad` shows failed fetches and unparseable payloads. Error
  messages include a payload preview.
- `gats status` shows backfill progress.

## 0.1.1 (2026-09-26)
- BSE serves one day per query (`{}` for ranges); jobs query single days.
- Attachment storage policy: material categories only, ≤ 5 MB each.

## 0.1.0 (2026-09-26)
- M1 data recorder: announcements, attachments, EOD + delivery, bands,
  instruments; point-in-time storage; probe/backfill/reparse CLI.
