# Changelog

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
