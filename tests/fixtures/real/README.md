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
