# Data sources

Only sources verified against live responses are listed as VERIFIED. Every
new source gets a row here before code depends on it.

| Source | Endpoint (configurable in `.env`) | Status | Notes |
|---|---|---|---|
| BSE announcements | `https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w` | VERIFIED 2026-09-26 | Details below. |
| NSE announcements | `https://www.nseindia.com/api/corporate-announcements?index=equities&from_date=DD-MM-YYYY&to_date=DD-MM-YYYY` | VERIFIED 2026-09-26 | Details below. |
| NSE EOD + delivery | `https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_DDMMYYYY.csv` | VERIFIED 2026-09-26 | 3,507 rows (2026-09-25). `DELIV_*` is `-` for non-EQ series. Available after ~18:00 IST. 404 on holidays. |
| NSE price bands | `https://nsearchives.nseindia.com/content/equities/sec_list.csv` | VERIFIED 2026-09-26 | Current file only (3,556 rows), so history exists only from the first recording. |
| NSE instruments | `https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv` | VERIFIED 2026-09-26 | Symbol ↔ ISIN, listing date, face value (2,585 rows). Current file only. |
| BSE attachments | `https://www.bseindia.com/xml-data/corpfiling/AttachLive/<file>` (falls back to `AttachHis/`) | VERIFIED 2026-09-26 | PDFs, often large. Stored per the attachment policy. |

## BSE announcements

- 50 rows per page, newest first, with `TotalPageCnt` in each row.
- **One day per query only**: a multi-day range returns `{}`.
- **Throttles bursts** (2026-09-28): it answers `{}` or an HTML block page.
  The client is limited to one request per 2 s, and pages are retried.
- Fields used: `NEWSID`, `SCRIP_CD`, `SLONGNAME`, `CATEGORYNAME`,
  `SUBCATNAME`, `NEWSSUB`, `HEADLINE`, `ATTACHMENTNAME`, `DissemDT`,
  `News_submission_dt`, `NEWS_DT`.
- Unmapped: `agenda_id`, `announcement_type`, `audio_video_file`,
  `bsenewsid`, `criticalnews`, `datainsdate`, `filestatus`, `fld_attachsize`,
  `investor_presentation`, `more`, `nsurl`, `old`, `quarter_id`, `recordid`,
  `rn`, `timediff`, `xml_name`.

## NSE announcements

- Needs session cookies from the homepage (warm-up).
- Returns the whole day in one response (~430–1,100 rows, up to ~300 KB).
- Observed live lag ~150 s at a 120 s poll.
- Fields used: `seq_id`, `symbol`, `sm_isin`, `sm_name`, `desc`,
  `attchmntText`, `attchmntFile`, `an_dt`, `exchdisstime`.
- Unmapped (T1.3): `attfilesize`, `bflag`, `csvname`, `difference`, `dt`,
  `filesize`, `hasxbrl`, `old_new`, `orgid`.

## To verify (candidates; do not rely on these until probed)

| Need | Candidate | Task |
|---|---|---|
| BSE scrip → ISIN | bseindia.com "List of Scrips" API; BSE bhavcopy (newer formats) | T2.1 |
| NSE symbol changes | `nsearchives.nseindia.com/content/equities/symbolchange.csv` | T2.2 |
| Index closes | `nsearchives.nseindia.com/content/indices/ind_close_all_DDMMYYYY.csv` | T2.7 |
| Holidays | NSE holiday list API | T2.6 |
| ASM/GSM lists | NSE/BSE surveillance pages | T2.9 |
| Minute candles | Upstox historical candle API v3 (1-min since Jan 2022, per its docs) | T5.1 |

## History depth (T1.4)

To be filled: the earliest verified date per source.
