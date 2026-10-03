"""Database schema (SQLAlchemy Core). Portable across SQLite and Postgres.

Point-in-time model:
- ``event_ts``: when the thing happened in the world (e.g. exchange
  dissemination time of a filing).
- ``available_at``: when *this system* could first have known it. Live rows
  use the time we first fetched them. Backfilled rows use ``event_ts`` and are
  flagged ``ingest_mode='backfill'`` so research can treat them differently.

Research code must filter on ``available_at``, never ``event_ts``; see
``gats.pit``.

Every parsed row points at the raw payload it came from (``raw_doc_id``), so
parsers can be fixed and re-run with ``gats reparse`` without refetching.
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)

from gats.db.types import UTCDateTime

SCHEMA_VERSION = 10

metadata = MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_N_name)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)

schema_meta = Table(
    "schema_meta",
    metadata,
    Column("key", String(64), primary_key=True),
    Column("value", Text, nullable=False),
)

raw_documents = Table(
    "raw_documents",
    metadata,
    Column("doc_id", String(64), primary_key=True),  # sha256 of the payload bytes
    Column("kind", String(32), nullable=False),  # e.g. bse_ann, nse_ann, eod, bands
    Column("source", String(16), nullable=False),
    Column("url", Text, nullable=False),
    Column("content_type", String(128)),
    Column("size_bytes", BigInteger, nullable=False),
    Column("blob_path", Text, nullable=False),  # relative to the raw store root
    Column("first_fetched_at", UTCDateTime, nullable=False),
    Column("meta", JSON, nullable=False, default=dict),
    Index(None, "kind", "first_fetched_at"),
)

fetch_log = Table(
    "fetch_log",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("job", String(64), nullable=False),
    Column("url", Text, nullable=False),
    Column("started_at", UTCDateTime, nullable=False),
    Column("elapsed_ms", Integer),
    Column("http_status", Integer),
    Column("ok", Boolean, nullable=False),
    Column("error", Text),
    Column("doc_id", String(64)),
    Column("n_records", Integer),
    Column("n_new", Integer),
    Index(None, "job", "started_at"),
    Index(None, "url"),
)

announcements = Table(
    "announcements",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("source", String(8), nullable=False),  # BSE | NSE
    Column("source_ann_id", String(64), nullable=False),
    Column("symbol", String(64)),  # NSE trading symbol
    Column("scrip_code", String(16)),  # BSE scrip code
    Column("isin", String(12)),
    Column("company_name", Text),
    Column("category", Text),
    Column("subcategory", Text),
    Column("subject", Text),
    Column("details", Text),
    Column("attachment_url", Text),
    Column("attachment_size", BigInteger),  # v5; see AnnouncementRecord
    Column("attachment_status", String(16), nullable=False),  # none|pending|done|missing|failed
    Column("attachment_attempts", Integer, nullable=False, default=0),
    Column("attachment_doc_id", String(64), ForeignKey("raw_documents.doc_id")),
    Column("exch_submitted_ts", UTCDateTime),
    Column("exch_disseminated_ts", UTCDateTime),
    Column("event_ts", UTCDateTime, nullable=False),
    Column("available_at", UTCDateTime, nullable=False),
    Column("ingest_mode", String(16), nullable=False),  # live | catchup | backfill
    Column("first_seen_at", UTCDateTime, nullable=False),
    Column("raw_doc_id", String(64), ForeignKey("raw_documents.doc_id"), nullable=False),
    Column("parser_version", String(32), nullable=False),
    UniqueConstraint("source", "source_ann_id"),
    Index(None, "event_ts"),
    Index(None, "available_at"),
    Index(None, "isin"),
    Index(None, "symbol"),
    Index(None, "scrip_code"),
    Index(None, "attachment_status"),
)

eod_prices = Table(
    "eod_prices",
    metadata,
    Column("trade_date", Date, primary_key=True),
    Column("symbol", String(64), primary_key=True),
    Column("series", String(8), primary_key=True),
    Column("prev_close", Float),
    Column("open", Float),
    Column("high", Float),
    Column("low", Float),
    Column("last", Float),
    Column("close", Float),
    Column("avg_price", Float),
    Column("volume", BigInteger),
    Column("turnover_lacs", Float),
    Column("num_trades", BigInteger),
    Column("deliv_qty", BigInteger),
    Column("deliv_pct", Float),
    Column("available_at", UTCDateTime, nullable=False),
    Column("raw_doc_id", String(64), ForeignKey("raw_documents.doc_id"), nullable=False),
    Column("parser_version", String(32), nullable=False),
    Index(None, "symbol", "trade_date"),
)

price_bands = Table(
    "price_bands",
    metadata,
    Column("as_of_date", Date, primary_key=True),
    Column("symbol", String(64), primary_key=True),
    Column("series", String(8), primary_key=True),
    Column("band", String(16)),
    Column("remarks", Text),
    Column("available_at", UTCDateTime, nullable=False),
    Column("raw_doc_id", String(64), ForeignKey("raw_documents.doc_id"), nullable=False),
    Column("parser_version", String(32), nullable=False),
)

instrument_snapshots = Table(
    "instrument_snapshots",
    metadata,
    Column("as_of_date", Date, primary_key=True),
    Column("symbol", String(64), primary_key=True),
    Column("series", String(8), primary_key=True),
    Column("isin", String(12)),
    Column("name", Text),
    Column("listing_date", Date),
    Column("face_value", Float),
    Column("market_lot", Integer),
    Column("available_at", UTCDateTime, nullable=False),
    Column("raw_doc_id", String(64), ForeignKey("raw_documents.doc_id"), nullable=False),
    Column("parser_version", String(32), nullable=False),
    Index(None, "isin"),
)

# v2: per-day backfill bookkeeping. A day is skipped by later backfills only
# once it is `complete`; `gave_up` stops automatic retries (manual backfill
# still retries it).
# v3: `expected_records` (BSE's ROWCNT; NSE's row count) so completeness can be
# audited as n_records >= expected_records.
backfill_days = Table(
    "backfill_days",
    metadata,
    Column("source", String(8), primary_key=True),
    Column("day", Date, primary_key=True),
    Column("status", String(16), nullable=False),  # complete | incomplete | gave_up
    Column("attempts", Integer, nullable=False),
    Column("n_records", Integer),
    Column("expected_records", Integer),
    Column("last_error", Text),
    Column("updated_at", UTCDateTime, nullable=False),
)


# --- v4: reference data (M2) ---------------------------------------------------
# Every application of a current-only reference file (one that the source
# republishes in full and keeps no history of) is logged here. A version row
# below is known to hold on every logged date between its valid_from and
# valid_to.
refdata_snapshots = Table(
    "refdata_snapshots",
    metadata,
    Column("kind", String(32), primary_key=True),
    Column("as_of_date", Date, primary_key=True),
    Column("n_records", Integer, nullable=False),
    Column("n_changes", Integer, nullable=False),
    Column("available_at", UTCDateTime, nullable=False),
    Column("raw_doc_id", String(64), ForeignKey("raw_documents.doc_id"), nullable=False),
    Column("parser_version", String(32), nullable=False),
)

# BSE scrip master as versions (type-2 slowly changing dimension): a new row
# only when a scrip's attributes change. 11k scrips as daily snapshots would
# cost ~4M rows a year for almost no information.
bse_scrips = Table(
    "bse_scrips",
    metadata,
    Column("scrip_code", String(16), primary_key=True),
    Column("valid_from", Date, primary_key=True),  # first snapshot showing this version
    Column("valid_to", Date),  # first snapshot no longer showing it; NULL = current
    Column("symbol", String(64)),
    Column("name", Text),
    Column("issuer_name", Text),
    Column("isin", String(12)),
    Column("status", String(16)),
    Column("scrip_group", String(8)),
    Column("face_value", Float),
    Column("segment", String(32)),
    Column("industry", Text),
    Column("available_at", UTCDateTime, nullable=False),  # when this version was first seen
    Column("raw_doc_id", String(64), ForeignKey("raw_documents.doc_id"), nullable=False),
    Column("parser_version", String(32), nullable=False),
    Index(None, "isin"),
    Index(None, "symbol"),
)

# NSE's cumulative symbol-change file (history since 1999). Rows already in
# the first fetch get available_at = the effective date at 00:00 IST (NSE
# announces changes before they take effect); rows that appear later get
# the fetch time.
nse_symbol_changes = Table(
    "nse_symbol_changes",
    metadata,
    Column("old_symbol", String(64), primary_key=True),
    Column("new_symbol", String(64), primary_key=True),
    Column("effective_date", Date, primary_key=True),
    Column("company_name", Text),
    Column("available_at", UTCDateTime, nullable=False),
    Column("raw_doc_id", String(64), ForeignKey("raw_documents.doc_id"), nullable=False),
    Column("parser_version", String(32), nullable=False),
    Index(None, "new_symbol", "effective_date"),
)

# Security master (T2.3). Derived from the reference tables by
# `gats refdata build`; rows are upserted and stamped with the build that
# produced them, and lookups use the latest build only, so a rebuild never
# needs to delete. security_id values are stable across builds.
refdata_builds = Table(
    "refdata_builds",
    metadata,
    Column("build_id", Integer, primary_key=True, autoincrement=True),
    Column("built_at", UTCDateTime, nullable=False),
    Column("stats", JSON, nullable=False, default=dict),
)

securities = Table(
    "securities",
    metadata,
    Column("security_id", Integer, primary_key=True, autoincrement=True),
    Column("primary_isin", String(12)),  # latest ISIN seen for the security
    Column("name", Text),
    Column("merged_into", Integer),  # set when two components were joined
    Column("first_build_id", Integer, nullable=False),
    Column("last_build_id", Integer, nullable=False),
    Index(None, "primary_isin"),
)

# Identifier validity windows [valid_from, valid_to). OPEN_START (1900-01-01)
# marks "before any recorded change"; valid_to NULL marks "still valid".
security_identifiers = Table(
    "security_identifiers",
    metadata,
    Column("id_type", String(16), primary_key=True),  # isin | nse_symbol | bse_scrip
    Column("value", String(64), primary_key=True),
    Column("valid_from", Date, primary_key=True),
    Column("security_id", Integer, nullable=False),
    Column("valid_to", Date),
    Column("source", String(32), nullable=False),
    # Earliest time the system could have tied this identifier to the security.
    Column("available_at", UTCDateTime, nullable=False),
    Column("last_build_id", Integer, nullable=False),
    Index(None, "security_id"),
)

# Which security each filing is about (T2.4). Unresolved filings get a row
# with security_id NULL, so they are retried only when a newer master build
# exists instead of on every pass.
announcement_security = Table(
    "announcement_security",
    metadata,
    Column("announcement_id", Integer, ForeignKey("announcements.id"), primary_key=True),
    Column("security_id", Integer),
    Column("method", String(16)),  # isin | nse_symbol | bse_scrip
    Column("build_id", Integer, nullable=False),
    Column("linked_at", UTCDateTime, nullable=False),
    Index(None, "security_id"),
)

# The event each filing belongs to (T2.5): the same disclosure on BSE and NSE
# shares one event_group_id (the smaller announcement id of the pair).
announcement_event_group = Table(
    "announcement_event_group",
    metadata,
    Column("announcement_id", Integer, ForeignKey("announcements.id"), primary_key=True),
    Column("event_group_id", Integer, nullable=False),
    Column("rule_version", String(32), nullable=False),
    Column("grouped_at", UTCDateTime, nullable=False),
    Index(None, "event_group_id"),
)

# Per-day EOD bookkeeping (T2.6). A day is `loaded`, `not_published` (the
# file 404s: weekends, or before publication) or `other_day` (NSE served a
# copy of another session's file, which it does for weekday holidays). The
# recorder stops asking about a closed day after a few attempts, and the
# trading calendar is derived from these rows.
eod_days = Table(
    "eod_days",
    metadata,
    Column("trade_date", Date, primary_key=True),
    Column("status", String(16), nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("n_records", Integer, nullable=False),
    Column("file_date", Date),  # for other_day: the session the file really holds
    Column("http_status", Integer),
    Column("updated_at", UTCDateTime, nullable=False),
)

# NSE's published holiday list (current year only; see sources.nse_holidays).
# Cumulative: each year's list is added as it is fetched.
market_holidays = Table(
    "market_holidays",
    metadata,
    Column("segment", String(16), primary_key=True),  # CM = equity cash market
    Column("holiday_date", Date, primary_key=True),
    Column("description", Text),
    Column("available_at", UTCDateTime, nullable=False),
    Column("raw_doc_id", String(64), ForeignKey("raw_documents.doc_id"), nullable=False),
    Column("parser_version", String(32), nullable=False),
)

# NSE index closes (T2.7), one row per index per session.
index_eod = Table(
    "index_eod",
    metadata,
    Column("trade_date", Date, primary_key=True),
    Column("index_name", String(128), primary_key=True),
    Column("open", Float),
    Column("high", Float),
    Column("low", Float),
    Column("close", Float),
    Column("points_change", Float),
    Column("pct_change", Float),
    Column("volume", BigInteger),
    Column("turnover_cr", Float),
    Column("pe", Float),
    Column("pb", Float),
    Column("div_yield", Float),
    Column("available_at", UTCDateTime, nullable=False),
    Column("raw_doc_id", String(64), ForeignKey("raw_documents.doc_id"), nullable=False),
    Column("parser_version", String(32), nullable=False),
    Index(None, "index_name", "trade_date"),
)

# Same bookkeeping as eod_days, for the index file (which 404s on holidays).
index_days = Table(
    "index_days",
    metadata,
    Column("trade_date", Date, primary_key=True),
    Column("status", String(16), nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("n_records", Integer, nullable=False),
    Column("file_date", Date),
    Column("http_status", Integer),
    Column("updated_at", UTCDateTime, nullable=False),
)

# NSE corporate actions (T2.8). reported_symbol is the symbol NSE used when we
# fetched the row (the API reports today's symbol for old actions), so joins
# with prices translate it through the symbol history first.
corporate_actions = Table(
    "corporate_actions",
    metadata,
    Column("reported_symbol", String(64), primary_key=True),
    Column("ex_date", Date, primary_key=True),
    Column("subject", Text, primary_key=True),
    Column("series", String(8)),
    Column("isin", String(12)),
    Column("company", Text),
    Column("record_date", Date),
    Column("face_value", Float),
    Column("kind", String(16), nullable=False),
    Column("share_multiplier", Float),
    Column("cash_per_share", Float),
    Column("needs_review", Boolean, nullable=False),
    # min(first fetch, 00:00 IST on the ex-date): actions are announced
    # before they go ex, so a backfilled action was knowable by that morning.
    Column("available_at", UTCDateTime, nullable=False),
    Column("first_seen_at", UTCDateTime, nullable=False),
    Column("raw_doc_id", String(64), ForeignKey("raw_documents.doc_id"), nullable=False),
    Column("parser_version", String(32), nullable=False),
    Index(None, "ex_date"),
    Index(None, "isin"),
)

# NSE surveillance lists (T2.9) as versions, keyed "<list>:<symbol>" (e.g.
# "LTASM:A2ZINFRA"); valid_to is set when the stock leaves the list.
surveillance_versions = Table(
    "surveillance_versions",
    metadata,
    Column("entity_key", String(80), primary_key=True),
    Column("valid_from", Date, primary_key=True),
    Column("valid_to", Date),
    Column("list_name", String(8), nullable=False),  # LTASM | STASM | GSM
    Column("symbol", String(64), nullable=False),
    Column("isin", String(12)),
    Column("company", Text),
    Column("stage", String(32)),
    Column("surv_code", String(64)),
    Column("surv_desc", Text),
    Column("since", Date),
    Column("available_at", UTCDateTime, nullable=False),
    Column("raw_doc_id", String(64), ForeignKey("raw_documents.doc_id"), nullable=False),
    Column("parser_version", String(32), nullable=False),
    Index(None, "symbol"),
)

# Event type of each filing per taxonomy version (T3.1). A new taxonomy
# version adds rows; studies record the version they used.
announcement_event_types = Table(
    "announcement_event_types",
    metadata,
    Column("announcement_id", Integer, ForeignKey("announcements.id"), primary_key=True),
    Column("taxonomy_version", String(32), primary_key=True),
    Column("event_type", String(32), nullable=False),
    Column("rule_no", Integer),  # index of the matching rule; NULL for OTHER
    Column("classified_at", UTCDateTime, nullable=False),
    Index(None, "taxonomy_version", "event_type"),
)

# Text extracted from attachments (T4.1), per extractor version: a better
# extractor adds rows next to the old ones.
document_texts = Table(
    "document_texts",
    metadata,
    Column("doc_id", String(64), ForeignKey("raw_documents.doc_id"), primary_key=True),
    Column("extractor", String(32), primary_key=True),
    Column("extractor_version", String(16), primary_key=True),
    Column("pages", Integer, nullable=False),
    Column("chars", Integer, nullable=False),
    Column("needs_ocr", Boolean, nullable=False),
    Column("error", Text),
    Column("text", Text, nullable=False),
    Column("extracted_at", UTCDateTime, nullable=False),
)

# Every LLM call (T4.4): the cache, keyed by what determines the answer, and
# the log of how often the model fails validation.
llm_extractions = Table(
    "llm_extractions",
    metadata,
    Column("doc_id", String(64), ForeignKey("raw_documents.doc_id"), primary_key=True),
    Column("event_type", String(32), primary_key=True),
    Column("prompt_hash", String(16), primary_key=True),
    Column("model", String(128), primary_key=True),
    Column("status", String(24), nullable=False),  # ok | invalid_json | invalid_schema | ...
    Column("output", JSON),  # the validated OrderWin, when ok
    Column("raw", Text),  # the model's message, kept for failures too
    Column("error", Text),
    Column("latency_ms", Integer),
    Column("prompt_tokens", Integer),
    Column("output_tokens", Integer),
    Column("created_at", UTCDateTime, nullable=False),
)

# Final extracted facts per filing and extractor version (rules/LLM/cascade).
extractions = Table(
    "extractions",
    metadata,
    Column("announcement_id", Integer, ForeignKey("announcements.id"), primary_key=True),
    Column("extractor_version", String(160), primary_key=True),
    Column("event_type", String(32), nullable=False),
    Column("method", String(32), nullable=False),  # rules_annexure | rules_sentence | llm | ...
    Column("fields", JSON),
    Column("amount_inr", Float),
    Column("confidence", Float, nullable=False),
    Column("created_at", UTCDateTime, nullable=False),
    Index(None, "extractor_version", "event_type"),
)

# One-minute bars are Parquet files (M5); this records which instrument-
# months were fetched, so fetching is resumable. The current month stays
# "partial" until it is over.
bar_months = Table(
    "bar_months",
    metadata,
    Column("instrument_key", String(64), primary_key=True),
    Column("month", Date, primary_key=True),  # its first day
    Column("interval", String(8), primary_key=True),  # "1m"
    Column("status", String(16), nullable=False),  # complete | partial | failed
    Column("n_bars", Integer),
    Column("attempts", Integer, nullable=False),
    Column("raw_doc_id", String(64)),
    Column("last_error", Text),
    Column("updated_at", UTCDateTime, nullable=False),
)

# Every research run (T6.6): written before the run starts, so a crashed or
# abandoned run still counts. Distinct designs (params_hash) are the trial
# count behind the multiple-testing corrections.
experiments = Table(
    "experiments",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("kind", String(32), nullable=False),  # event_study | backtest
    Column("name", String(128), nullable=False),  # study or strategy
    Column("params_hash", String(64), nullable=False),
    Column("params", JSON),
    Column("data_start", Date),
    Column("data_end", Date),
    Column("holdout", Boolean, nullable=False),  # read the test period
    Column("git_sha", String(40)),
    Column("git_dirty", Boolean),
    Column("status", String(16), nullable=False),  # running | done | failed
    Column("metrics", JSON),
    Column("error", Text),
    Column("started_at", UTCDateTime, nullable=False),
    Column("finished_at", UTCDateTime),
    Index(None, "kind", "name"),
)

# Quarterly results filings and the revenue their XBRL states (T4.7). One row
# per filing: a revised filing is a new row (the exchange's ``seq``), so what
# was known at any moment can be read back. Rows without a dissemination time
# (pre-2010s) are not stored: they could never be read point-in-time.
financial_results = Table(
    "financial_results",
    metadata,
    Column("symbol", String(64), primary_key=True),
    Column("period_end", Date, primary_key=True),
    Column("consolidated", Boolean, primary_key=True),
    Column("seq", String(32), primary_key=True),
    Column("regime", String(16), nullable=False),  # legacy | integrated
    Column("period_start", Date),
    Column("audited", Boolean, nullable=False),
    Column("revised", Boolean, nullable=False),
    Column("isin", String(12)),
    Column("xbrl_url", Text),
    Column("xbrl_status", String(16), nullable=False),  # pending | done | failed | none
    Column("xbrl_attempts", Integer, nullable=False),
    Column("xbrl_note", Text),
    Column("xbrl_doc_id", String(64)),
    Column("revenue", Float),  # RevenueFromOperations for the quarter, rupees
    Column("event_ts", UTCDateTime, nullable=False),
    Column("available_at", UTCDateTime, nullable=False),
    Column("raw_doc_id", String(64), ForeignKey("raw_documents.doc_id"), nullable=False),
    Column("parser_version", String(32), nullable=False),
    Index(None, "symbol", "available_at"),
    Index(None, "xbrl_status"),
)

# --- paper trading (M7) --------------------------------------------------------
# A paper run is one trading system (``design_hash``) run forward in time.
paper_runs = Table(
    "paper_runs",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", String(64), nullable=False, unique=True),
    Column("strategy", String(128), nullable=False),
    Column("design_hash", String(64), nullable=False),
    Column("design", JSON, nullable=False),
    Column("git_sha", String(40)),
    Column("git_dirty", Boolean),
    Column("created_at", UTCDateTime, nullable=False),
)

# Everything the paper engine was given, in order, with the moment it was
# handed over and what the risk engine was told about the outside world.
# Replaying it rebuilds the account, so this is the run's source of truth.
paper_journal = Table(
    "paper_journal",
    metadata,
    Column("run_id", Integer, ForeignKey("paper_runs.id"), primary_key=True),
    Column("seq", Integer, primary_key=True),
    Column("kind", String(8), nullable=False),  # event | bar | ref | close
    Column("item_key", String(160), nullable=False),  # the same input is taken once
    Column("at", UTCDateTime, nullable=False),
    Column("payload", JSON, nullable=False),
    Column("answers", JSON),
    UniqueConstraint("run_id", "item_key"),
)

# What the engine did, written as it happened. A replay must reproduce these
# rows exactly, which is how changed code is caught before it rewrites a
# paper track record.
paper_orders = Table(
    "paper_orders",
    metadata,
    Column("run_id", Integer, ForeignKey("paper_runs.id"), primary_key=True),
    Column("order_id", Integer, primary_key=True),
    Column("instrument_key", String(64), nullable=False),
    Column("side", String(4), nullable=False),
    Column("product", String(16), nullable=False),
    Column("quantity", Integer, nullable=False),
    Column("limit_price", Float, nullable=False),
    Column("closes", Boolean, nullable=False),
    Column("reason", Text, nullable=False),
    Column("submitted_at", UTCDateTime, nullable=False),
    Column("eligible_at", UTCDateTime, nullable=False),
    Column("expires_at", UTCDateTime),
    Column("filled", Integer, nullable=False),
    Column("status", String(16), nullable=False),  # working | filled | expired | rejected
    Column("note", Text, nullable=False),
    Index(None, "run_id", "status"),
)

paper_executions = Table(
    "paper_executions",
    metadata,
    Column("run_id", Integer, ForeignKey("paper_runs.id"), primary_key=True),
    Column("seq", Integer, primary_key=True),
    Column("order_id", Integer, nullable=False),  # 0: closed by the engine at the day's end
    Column("instrument_key", String(64), nullable=False),
    Column("side", String(4), nullable=False),
    Column("product", String(16), nullable=False),
    Column("quantity", Integer, nullable=False),
    Column("price", Float, nullable=False),
    Column("at", UTCDateTime, nullable=False),
    Column("charges", Float, nullable=False),
    Column("reason", Text, nullable=False),
    Column("bar_volume", Integer, nullable=False),
)
