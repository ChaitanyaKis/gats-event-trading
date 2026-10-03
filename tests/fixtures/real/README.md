# Real exchange samples

Trimmed copies of live payloads captured with `gats probe`, made with
`scripts/trim_fixture.py`. Rows are kept byte-for-byte (CSV) or object-for-
object (JSON), so the parsers see exactly what the exchanges send. Public
exchange data only; nothing personal.

| File | Source URL | Fetched (UTC) | Trim |
|---|---|---|---|
| `bse_ann_2026-10-02.json` | `api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?pageno=1&strPrevDate=20261002&strToDate=20261002&...` | 2026-10-02 16:37 | first 30 of 50 rows on page 1 (6 pages, 272 rows that day) |
| `nse_ann_2026-10-02.json` | `www.nseindia.com/api/corporate-announcements?index=equities&from_date=02-10-2026&to_date=02-10-2026` | 2026-10-02 16:37 | first 30 of 110 rows |
| `nse_eod_2026-10-01.csv` | `nsearchives.nseindia.com/products/content/sec_bhavdata_full_01102026.csv` | 2026-10-02 16:37 | 30 of 3,534 rows; every SERIES present |
| `nse_bands_2026-10-02.csv` | `nsearchives.nseindia.com/content/equities/sec_list.csv` | 2026-10-02 16:37 | 30 of 3,574 rows; every Series, Band and Remarks value present |
| `nse_instruments_2026-10-02.csv` | `nsearchives.nseindia.com/content/equities/EQUITY_L.csv` | 2026-10-02 16:37 | 30 of 2,593 rows; every SERIES present |
| `bse_ann_2023-10-03_page1.json` | same endpoint, `strPrevDate=strToDate=20231003`, page 1 | 2026-10-02 16:43 | first 30 of 50 rows; past-day shape (no `TotalPageCnt`, ROWCNT 996) |
| `bse_scrips_2026-10-02.json` | `api.bseindia.com/BseIndiaAPI/api/ListofScripData/w?Group=&Scripcode=&industry=&segment=Equity&status=` | 2026-10-02 17:10 | 30 of 10,918 rows chosen to cover every Status, Segment, `NA`/empty ISINs and the two invalid ISINs |
| `nse_symbolchange_2026-10-02.csv` | `nsearchives.nseindia.com/content/equities/symbolchange.csv` | 2026-10-02 17:20 | 12 of 1,065 lines: first two, real rename chains (ZOMATO→ETERNAL, LTI→LTIM→LTM, TELCO→TATAMOTORS→TMPV, …), a self-map and an empty company name |
| `nse_eod_2025-04-08_excerpt.csv`, `nse_eod_2025-04-09_excerpt.csv` | `sec_bhavdata_full_08042025.csv`, `…_09042025.csv` | 2026-10-02 17:20 | header + ZOMATO/ETERNAL and RELIANCE rows: the day before and the day of the ZOMATO→ETERNAL change |
| `nse_ann_2024-08-01_eternal.json` | NSE announcements API, `from_date=to_date=01-08-2024` | 2026-10-02 17:07 | 3 Zomato filings from 2024, reported under the *current* symbol ETERNAL |
| `nse_indices_2026-10-01.csv` | `nsearchives.nseindia.com/content/indices/ind_close_all_01102026.csv` | 2026-10-03 | header + benchmark rows (Nifty 50/500, Midcap 150, Smallcap 250, Bank, IT, Pharma, Microcap 250, India VIX) and rows with `-` values |
| `ca_eod_cases_2025-09.json` | `sec_bhavdata_full` for the previous session and the ex-date of 10 splits/bonuses (Sep 2025) | 2026-10-03 | the EQ line of each symbol on both days, plus the header |
| `nse_corp_actions_sample.json` | `www.nseindia.com/api/corporates-corporateActions?index=equities&...` (2019-09 → 2026-09) | 2026-10-03 | the 10 cases' Sep 2025 rows, plus real examples of a hyphenated bonus, a bonus of preference shares, a consolidation, rights, a demerger, and an LTI dividend reported under today's symbol LTM |
| `nse_asm_2026-10-03.json` | `www.nseindia.com/api/reportASM` | 2026-10-03 | 9 of 126 long-term entries (all stages) and 5 of 68 short-term |
| `nse_gsm_2026-10-03.json` | `www.nseindia.com/api/reportGSM` | 2026-10-03 | 8 of 77 entries, incl. stages `0` and `LXII` |
| `order_win_attachment.pdf` | Goldiam International order-win attachment (the same file on NSE and BSE), via `gats extract fetch` | 2026-10-03 | unchanged, 180 KB, 2 pages |
| `order_win_texts.json` | text of 5 real order-win attachments (HEC Infra, Ceinsys, NBCC, Veerhealth, Goldiam), extracted with pypdf | 2026-10-03 | whitespace-normalised full text |
