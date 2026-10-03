# Data sources

Only sources verified against live responses are listed as VERIFIED. Every
new source gets a row here before code depends on it.

| Source | Endpoint (configurable in `.env`) | Status | Notes |
|---|---|---|---|
| BSE announcements | `https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w` | VERIFIED 2026-09-26, re-probed 2026-10-02 | Details below. Real sample: `tests/fixtures/real/bse_ann_2026-10-02.json`. |
| NSE announcements | `https://www.nseindia.com/api/corporate-announcements?index=equities&from_date=DD-MM-YYYY&to_date=DD-MM-YYYY` | VERIFIED 2026-09-26, re-probed 2026-10-02 | Details below. Real sample: `tests/fixtures/real/nse_ann_2026-10-02.json`. |
| NSE EOD + delivery | `https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_DDMMYYYY.csv` | VERIFIED 2026-09-26 | 3,507 rows (2026-09-25). `DELIV_*` is `-` for non-EQ series. Available after ~18:00 IST. **`PREV_CLOSE` is NOT adjusted for splits/bonuses** (T2.8: on 10/10 real ex-dates in Sep 2025, `PREV_CLOSE` = the previous session's raw close; e.g. PIDILITIND 1:1 bonus: 3038.00 → close 1489.30), so returns use corporate-action multipliers. **Weekday holidays do not 404**: the holiday's URL serves a copy of the previous session's file (Sundays do the same with Friday's file; Saturdays 404) (verified 2026-10-03: `_14092026` holds 11-Sep-2026 rows, `_02102026` holds 01-Oct-2026, `_22102025` holds 21-Oct-2025); weekends 404. Special sessions publish their own file (Diwali Muhurat 21-Oct-2025, Sunday budget session 01-Feb-2026). Re-probed 2026-10-02 (3,534 rows for 2026-10-01; series EQ, SM, BE, ST, GS, GB, BZ, IV, RR, E1, N1). |
| NSE price bands | `https://nsearchives.nseindia.com/content/equities/sec_list.csv` | VERIFIED 2026-09-26 | Current file only (3,556 rows), so history exists only from the first recording. Re-probed 2026-10-02 (3,574 rows). `Remarks` carries GSM stages (`GSM STAGE - 0` … `IV`), `-` otherwise; `Band` is 2/5/10/20/40 or `No Band`. |
| NSE instruments | `https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv` | VERIFIED 2026-09-26 | Symbol ↔ ISIN, listing date, face value (2,585 rows). Current file only. Re-probed 2026-10-02 (2,593 rows; series EQ, BE, BZ). |
| BSE scrip list (scrip code → ISIN) | `https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w?Group=&Scripcode=&industry=&segment=Equity&status=` | VERIFIED 2026-10-02 | Details below. Real sample: `tests/fixtures/real/bse_scrips_2026-10-02.json`. |
| BSE equity bhavcopy (UDiFF) | `https://www.bseindia.com/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_YYYYMMDD_F_0000.CSV` | VERIFIED 2026-10-02 (one day, not yet used) | 200 for 2026-10-01, 884 KB. Columns `TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,…,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,…,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,…`. `FinInstrmId` = scrip code, `SctySrs` = group. History depth not probed. |
| NSE symbol changes | `https://nsearchives.nseindia.com/content/equities/symbolchange.csv` | VERIFIED 2026-10-02 | Cumulative, **headerless**: company, old symbol, new symbol, effective date (`DD-MON-YYYY`); 1,065 rows 1999-09-15 → 2026-09-25. Effective date = first session under the new symbol (EOD has ZOMATO on 08-Apr-2025, ETERNAL on 09-Apr-2025; PREV_CLOSE carries over). Some self-maps (old = new) on debt/ETF rows. Real sample: `tests/fixtures/real/nse_symbolchange_2026-10-02.csv`. |
| NSE name changes | `https://nsearchives.nseindia.com/content/equities/namechange.csv` | VERIFIED 2026-10-02 (not used yet) | Header `NCH_SYMBOL, NCH_PREV_NAME, NCH_NEW_NAME, NCH_DT`; 2,328 rows. |
| NSE holiday list | `https://www.nseindia.com/api/holiday-master?type=trading` (warm-up cookies; Referer the holidays page) | VERIFIED 2026-10-03 | JSON keyed by segment (`CM` = equity cash, `FO`, `CD`, `COM`, …); entries `tradingDate` (`DD-Mon-YYYY`), `weekDay`, `description`, `morning_session`/`evening_session` (null on every 2026 row, even `Diwali Laxmi Pujan*`), `Sr_no`. **Current year only** (20 CM dates for 2026, weekend holidays included). |
| NSE market hours | `https://www.nseindia.com/static/market-data/market-timings` | VERIFIED 2026-10-03 | Capital market: pre-open 09:00–09:08, normal market **09:15–15:30**, closing session 15:40–16:00. Used as `GATS_SESSION_OPEN_IST` / `_CLOSE_IST`. |
| NSE index closes | `https://nsearchives.nseindia.com/content/indices/ind_close_all_DDMMYYYY.csv` | VERIFIED 2026-10-03 | Header `Index Name, Index Date (DD-MM-YYYY), Open/High/Low/Closing Index Value, Points Change, Change(%), Volume, Turnover (Rs. Cr.), P/E, P/B, Div Yield`; `-` = not applicable. 167 indices (2026-10-01), 79 (2019-10-01). Weekends and holidays **404** (cleaner than the bhavcopy); special sessions have files. Files back to ≥ 2012 but names change (`S&P CNX Nifty` 2012 → `CNX Nifty` 2013 → `Nifty 50`); benchmark names stable 2019→2026. One year backfilled: 248 sessions 2025-10-01 → 2026-10-01. Real sample: `tests/fixtures/real/nse_indices_2026-10-01.csv`. |
| NSE corporate actions | `https://www.nseindia.com/api/corporates-corporateActions?index=equities&from_date=DD-MM-YYYY&to_date=DD-MM-YYYY` (warm-up cookies) | VERIFIED 2026-10-03 | JSON list: `symbol, series, isin, comp, subject, exDate, recDate, faceVal, bc*/nd* dates, caBroadcastDate, ind`. One-year ranges work (1,955–2,706 rows/yr, 2019→2026; 17,019 stored). **`symbol`/`comp` are as of the fetch** (LTI's 2019 dividend appears under LTM; Tata Motors' 2023 dividend under TMPV). `subject` is free text: `Bonus a:b` (also `Bonus- a:b`), `Face Value Split (Sub-Division) - From Rs X/- Per Share To Re/Rs Y/- Per Share`, `Consolidation Of Equity Shares From Re 1 … To Rs 10`, `Rights a:b @ Premium Rs P/-`, `Demerger`, `Scheme Of Arrangement - Bonus Ncrps a:b` (also misspelt `Arangement`), dividends. Real sample: `tests/fixtures/real/nse_corp_actions_sample.json`. |
| NSE ASM lists | `https://www.nseindia.com/api/reportASM` (warm-up cookies) | VERIFIED 2026-10-03 | `{"longterm": {"data": [...]}, "shortterm": {"data": [...]}}`; rows `symbol, isin, companyName, asmSurvIndicator ("Stage I"…"Stage IV"), survCode ("LTASM - I (13)"), survDesc, asmTime (DD-Mon-YYYY), series (null), srno`. 126 long-term + 68 short-term on 2026-10-03. Current-only. Real sample: `tests/fixtures/real/nse_asm_2026-10-03.json`. |
| NSE GSM list | `https://www.nseindia.com/api/reportGSM` | VERIFIED 2026-10-03 | List of `symbol, isin, companyName, gsmStage ("0", "LXII", …), survCode ("IBC I & GSM 0 (58)"), survDesc, gsmTime (DD-Mon-YYYY HH:MM:SS), srno`; 77 entries. Current-only. |
| NSE series legend | `https://www.nseindia.com/static/market-data/legend-of-series` | VERIFIED 2026-10-03 | Fully paid equity: **EQ** rolling, **BE/BZ** trade-for-trade; SME: **SM** rolling, **ST/SZ** trade-for-trade. BE = moved to T2T (surveillance); BZ/SZ = T2T for non-compliance (SEBI CIR/MRD/DSA/31/2013). |
| Local LLM (Ollama) | `POST http://localhost:11434/api/chat` (`GATS_OLLAMA_URL`) | VERIFIED 2026-10-03 (Ollama 0.32.8) | Request `{model, messages, format: <JSON schema>, options: {temperature: 0, seed: 0}, stream: false}`; the reply's `message.content` is a JSON string shaped by the schema, with token counts in `prompt_eval_count` and `eval_count`. Models (`GET /api/tags`): `qwen2.5-coder:7b-instruct-q4_K_M` (used), `qwen2.5-coder:3b-instruct-q4_K_M`. 20 real order-win filings: 20/20 schema-valid JSON, 18.3 s per call on average (laptop GPU), ~1,316 prompt and ~135 output tokens. Local, so no rate limit and no keys. |
| Upstox instrument file | `https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz` (`GATS_UPSTOX_INSTRUMENTS_URL`) | VERIFIED 2026-10-03 | Gzipped JSON list, no login, 1.86 MB, refreshed daily ~06:00 IST (Last-Modified 05:24 IST). 74,201 rows: NSE_FO 34,348, NSE_COM 21,423, **NSE_EQ 9,793** (EQ 2,688, SG 4,328, N0 1,028, SM 477, BE 237, ...), NCD_FO 8,498, **NSE_INDEX 139**. Equity `instrument_key` = `NSE_EQ|<ISIN>` (joins the security master; survives ZOMATO→ETERNAL, TATAMOTORS→TMPV); index key = `NSE_INDEX|<name>` (`NSE_INDEX|Nifty 50`). Fields: segment, name, exchange, isin, instrument_type (= series), instrument_key, exchange_token, trading_symbol, lot_size, tick_size (RELIANCE 10.0: apparently paise; not verified, not used), freeze_quantity, qty_multiplier, security_type, short_name (stale: ETERNAL = "Zomato"). Real sample: `tests/fixtures/real/upstox_NSE_instruments_2026-10-03.json`. |
| Upstox historical candles (V3) | `GET https://api.upstox.com/v3/historical-candle/{instrument_key, \| as %7C}/{unit}/{interval}/{to_date}/{from_date}` | DOCUMENTED 2026-10-03, **NOT PROBED** (needs the Analytics Token: Waiting on the human, item 6) | Per the docs: header `Authorization: Bearer <token>`; reply `{"status": "success", "data": {"candles": [[ts, open, high, low, close, volume, oi], ...]}}` with IST timestamps (`2025-01-01T00:00:00+05:30`). Minutes 1–300 from **January 2022**, at most **one month per request** (1–15 min); days from 2000. Rate limits per API and user: 50/s, 500/min, **2,000 per 30 min** (GATS: 1 request/s). The Analytics Token is read-only, valid 1 year, generated at `account.upstox.com/developer/apps#analytics`; it covers Historical Data and Market Quote without IP whitelisting and cannot trade. Sources: upstox.com/developer/api-documentation (v3/get-historical-candle-data, analytics-token, rate-limiting, instruments). |
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
- `Fld_Attachsize` → `attachment_size` (exact bytes; parser `bse-ann-v2`).
- Unmapped: `agenda_id`, `announcement_type`, `audio_video_file`,
  `bsenewsid`, `criticalnews`, `datainsdate`, `filestatus`,
  `investor_presentation`, `more`, `nsurl`, `old`, `quarter_id`, `recordid`,
  `rn`, `timediff`, `xml_name`.

## BSE scrip list

- Every equity scrip ever listed when `status` is empty: 10,918 rows on
  2026-10-02 (Active 5,069, Delisted 4,617, Suspended 1,229, `N` 3);
  `status=Active` gives 5,069. Segment `Equity` (9,952), `PreferenceShares`
  (123) or empty (843). REITs/InvITs are **not** included.
- Fields: `SCRIP_CD` (6 digits, unique), `scrip_id` (ticker), `Scrip_Name`,
  `Issuer_Name`, `Status`, `GROUP`, `FACE_VALUE`, `ISIN_NUMBER`, `Segment`,
  `INDUSTRY` (often null), `NSURL`, `Mktcap` (units undocumented; not stored).
- `ISIN_NUMBER`: 8,579 valid; `NA` 1,668, empty 669 (almost all delisted);
  two malformed (`INE546A1014`, `0`). 41 ISINs appear under two scrip codes
  (relistings).
- **Access:** bare clients get an Akamai 403; the API answers with
  browser-like headers (`Accept-Language`, `Referer`/`Origin`
  `https://www.bseindia.com`) after loading the BSE homepage, which
  `PoliteClient` does.
- Current-only: history is built from daily snapshots (`bse_scrips`
  versions, `refdata_snapshots` log) from 2026-10-02.

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
- **`symbol`, `sm_name` and `sm_isin` are as of the fetch, not the filing**
  (verified 2026-10-02): a Zomato filing of 2024-08-01 is returned as
  `ETERNAL` / `ETERNAL LIMITED`, though the symbol was ZOMATO until
  2025-04-08. Live rows carry the symbol of their day; backfilled rows carry
  the symbol of the backfill day. Resolve backfilled NSE symbols as of
  `first_seen_at` (`gats.refdata.symbols.SymbolHistory`) or through the ISIN.
- `attFileSize` → `attachment_size` in bytes (parser `nse-ann-v3`). It is a
  display size (`165.57 KB`, `1.27 MB`; `0 Bytes` = unknown), so it is exact
  only to the last shown digit. Verified 2026-10-03: for the same PDF filed
  on both exchanges, BSE's exact `Fld_Attachsize` / 1024 rounds to NSE's
  figure (169,542 bytes ↔ `165.57 KB`).
- Still unmapped (no research use yet): `fileSize` (repeats `attFileSize`),
  `hasXbrl` (always `true`), `smIndustry` (null on 60%), and `bflag`,
  `csvName`, `old_new`, `orgid` (always null).

## To verify (candidates; do not rely on these until probed)

| Need | Candidate | Task |
|---|---|---|
| BSE ASM/GSM lists | BSE surveillance pages (NSE lists cover most names) | later |
| Minute candles: a real reply | `gats probe upstox-candles --date <a session>` once the token exists; check bars vs NSE EOD | T5.1 |

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
