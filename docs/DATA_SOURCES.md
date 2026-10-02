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
- **Past days use a different row shape** (verified 2026-10-02, dates
  2012–2026): no `TotalPageCnt`, no `DataInsDate`/`RECORDID`, and
  `BSENewsid`/`Investor_Presentation` in place of `BSENEWSID`/
  `INVESTOR_PRESENTATION`. `Table1[0].ROWCNT` still gives the day's total, so
  the page count is `ceil(ROWCNT / 50)`. 0.1.2 read only `TotalPageCnt` and
  marked past days complete after one page; fixed in schema v3, which also
  reopens such days. Verified by a full backfill of 2023-10-03: 20 pages,
  996 distinct rows = ROWCNT.
- A day counts as complete only when every page was fetched **and** at
  least ROWCNT rows were collected (`backfill_days.expected_records`).
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

## History depth (T1.4, probed 2026-10-02)

One day probed per point (weekdays; 2 Oct is a market holiday). "✓" = 200
and the current parser reads it with no warnings.

| Probe date | BSE announcements | NSE announcements | NSE `sec_bhavdata_full` |
|---|---|---|---|
| 2026-09-02 (1 month) | ✓ 1,887 rows | ✓ 790 | ✓ 3,482 |
| 2025-10-01 (1 year) | ✓ 1,584 | ✓ 629 | ✓ 3,008 |
| 2024-10-01 (2 years) | ✓ 1,798 | ✓ 785 | ✓ 2,717 |
| 2023-10-03 (3 years) | ✓ 996 (full day backfilled) | ✓ 500 | ✓ 2,551 |
| 2021-10-01 (5 years) | ✓ 2,776 | ✓ 728 | ✓ 2,096 |
| 2018-10-01 (8 years) | ✓ 1,955 | ✓ 425 | 404 |
| 2015-10-01 | ✓ 854 | ✓ 440 | — |
| 2012-10-01 | ✓ 822 | ✓ 329 | — |

**Earliest verified dates**

| Source | Earliest verified | Caveats |
|---|---|---|
| BSE announcements | ≤ 2012-10-01 (deeper not probed) | `DissemDT` (to the ms) and `News_submission_dt` from 2018; 2015 has `DissemDT` but no submission time; 2012 has neither, so `event_ts` falls back to `NEWS_DT`. 2021-10-01: submission time on 31/50 rows. |
| NSE announcements | ≤ 2012-10-01 (deeper not probed) | `exchdisstime` exists from **≤ 2020-08-03** (absent on 2020-07-01 and earlier). Before that `an_dt` has **minute precision** (`HH:MM:00`) and is the only timestamp, so `event_ts` may be up to 59 s early; studies must treat those events as available at the end of the minute. |
| NSE `sec_bhavdata_full` | **2019-10-01** | Files 404 for every probed date up to 2019-09-27. The file named `sec_bhavdata_full_30092019.csv` exists but contains **27-Jun-2019** rows; the parser's DATE1 check rejects it (0 records). From 2019-10-01 DATE1 matches the file name (checked 01-Oct-2019 … 01-Jul-2020). |

Older EOD data needs a different source (candidates, **unverified**: NSE's
historical CM bhavcopy zips and separate delivery files). No parser is built
for them yet (T1.4 scope). For M3, ~7 years of EOD (Oct 2019 →) is
available, which bounds the event study's window.
