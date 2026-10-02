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

SCHEMA_VERSION = 4

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
