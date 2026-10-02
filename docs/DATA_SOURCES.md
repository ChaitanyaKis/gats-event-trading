# Data sources

Only sources verified against live responses are listed as VERIFIED. Every
new source gets a row here before code depends on it.

| Source | Endpoint (configurable in `.env`) | Status | Notes |
|---|---|---|---|
| BSE announcements | `https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w` | VERIFIED 2026-09-26, re-probed 2026-10-02 | Details below. Real sample: `tests/fixtures/real/bse_ann_2026-10-02.json`. |
| NSE announcements | `https://www.nseindia.com/api/corporate-announcements?index=equities&from_date=DD-MM-YYYY&to_date=DD-MM-YYYY` | VERIFIED 2026-09-26, re-probed 2026-10-02 | Details below. Real sample: `tests/fixtures/real/nse_ann_2026-10-02.json`. |
| NSE EOD + delivery | `https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_DDMMYYYY.csv` | VERIFIED 2026-09-26 | 3,507 rows (2026-09-25). `DELIV_*` is `-` for non-EQ series. Available after ~18:00 IST. 404 on holidays. Re-probed 2026-10-02 (3,534 rows for 2026-10-01; series EQ, SM, BE, ST, GS, GB, BZ, IV, RR, E1, N1). |
| NSE price bands | `https://nsearchives.nseindia.com/content/equities/sec_list.csv` | VERIFIED 2026-09-26 | Current file only (3,556 rows), so history exists only from the first recording. Re-probed 2026-10-02 (3,574 rows). `Remarks` carries GSM stages (`GSM STAGE - 0` … `IV`), `-` otherwise; `Band` is 2/5/10/20/40 or `No Band`. |
| NSE instruments | `https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv` | VERIFIED 2026-09-26 | Symbol ↔ ISIN, listing date, face value (2,585 rows). Current file only. Re-probed 2026-10-02 (2,593 rows; series EQ, BE, BZ). |
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
- **Timestamps (T1.3, parser `nse-ann-v2`, verified 2026-10-02):**
  - `an_dt` → `exch_submitted_ts` (exchange receipt time);
    `exchdisstime` → `exch_disseminated_ts` (= `event_ts`).
  - Evidence, 2,410 rows from 2026-09-25, 2026-09-30 and 2026-10-02:
    `difference` equals `exchdisstime − an_dt` **exactly in every row**
    (0 s: 342, 1 s: 1,791, 2 s: 275, 3 s: 2). NSE computes this lag from
    `an_dt`, so `an_dt` is the earlier exchange-side time, i.e. receipt. The
    parser warns if the identity ever breaks.
  - `dt` (`ddmmyyyyHHMMSS`) and `sort_date` (`YYYY-MM-DD HH:MM:SS`) equal
    `an_dt` in all 2,410 rows; they are redundant and not stored.
- Still unmapped (no research use yet): `attFileSize`/`fileSize` (identical
  human-readable sizes such as `1.27 MB`; `0 Bytes` on 37 rows),
  `hasXbrl` (always `true`), `smIndustry` (null on 60%), and `bflag`,
  `csvName`, `old_new`, `orgid` (always null).

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
