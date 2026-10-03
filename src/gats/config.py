"""Runtime configuration.

Values come from environment variables prefixed ``GATS_`` or a ``.env`` file in
the working directory. See ``.env.example`` for the full list.

Exchange endpoint URLs live here, not in code, because they could not be
verified from the build environment. If an exchange changes a URL or a probe
shows a different one, fix it in ``.env`` without touching code.
"""

from __future__ import annotations

from datetime import time
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GATS_", env_file=".env", extra="ignore")

    # Storage
    data_dir: Path = Path("data")
    db_url: str | None = None
    log_level: str = "INFO"

    # HTTP politeness
    user_agent: str = _DEFAULT_UA
    http_timeout_s: float = Field(default=20.0, gt=0)
    min_request_interval_s: float = Field(default=1.0, ge=0)
    # Slower per-host overrides. BSE's API throttled bursts on 2026-09-28.
    # Upstox allows 2,000 requests per 30 minutes per API and user (docs,
    # checked 2026-10-03): one a second stays under it.
    # Env var takes JSON: GATS_HOST_MIN_INTERVAL_S='{"api.bseindia.com": 3}'
    host_min_interval_s: dict[str, float] = Field(
        default_factory=lambda: {"api.bseindia.com": 2.0, "api.upstox.com": 1.0}
    )
    max_retries: int = Field(default=4, ge=0)
    backoff_base_s: float = Field(default=2.0, gt=0)
    backoff_max_s: float = Field(default=60.0, gt=0)

    # Endpoints. UNVERIFIED from the build sandbox: confirm with `gats probe`.
    bse_announcements_url: str = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
    bse_referer: str = "https://www.bseindia.com/"
    bse_attachment_live_base: str = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"
    bse_attachment_hist_base: str = "https://www.bseindia.com/xml-data/corpfiling/AttachHis/"
    nse_home_url: str = "https://www.nseindia.com/"
    nse_announcements_url: str = "https://www.nseindia.com/api/corporate-announcements"
    nse_announcements_referer: str = (
        "https://www.nseindia.com/companies-listing/corporate-filings-announcements"
    )
    nse_eod_url_template: str = (
        "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{ddmmyyyy}.csv"
    )
    nse_index_close_url_template: str = (
        "https://nsearchives.nseindia.com/content/indices/ind_close_all_{ddmmyyyy}.csv"
    )
    nse_bands_url: str = "https://nsearchives.nseindia.com/content/equities/sec_list.csv"
    nse_instruments_url: str = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
    nse_symbol_changes_url: str = (
        "https://nsearchives.nseindia.com/content/equities/symbolchange.csv"
    )
    nse_corp_actions_url: str = "https://www.nseindia.com/api/corporates-corporateActions"
    nse_corp_actions_referer: str = (
        "https://www.nseindia.com/companies-listing/corporate-filings-actions"
    )
    nse_asm_url: str = "https://www.nseindia.com/api/reportASM"
    nse_gsm_url: str = "https://www.nseindia.com/api/reportGSM"
    nse_surveillance_referer: str = "https://www.nseindia.com/reports/asm"
    nse_holidays_url: str = "https://www.nseindia.com/api/holiday-master"
    nse_holidays_referer: str = "https://www.nseindia.com/resources/exchange-communication-holidays"
    # Verified 2026-10-02 (needs browser headers + BSE homepage cookies).
    bse_scrips_url: str = "https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w"

    # Recorder schedule
    bse_enabled: bool = True
    bse_poll_s: float = Field(default=30.0, gt=0)
    bse_max_pages: int = Field(default=10, ge=1)
    # First poll after a (re)start pages further back: a busy day has 20+ pages.
    bse_catchup_max_pages: int = Field(default=100, ge=1)
    # BSE intermittently answers {} or an HTML block page under load.
    bse_page_retries: int = Field(default=2, ge=0)
    bse_page_retry_delay_s: float = Field(default=15.0, ge=0)
    bse_warmup: bool = True  # load the homepage once for session cookies
    nse_enabled: bool = True
    nse_poll_s: float = Field(default=120.0, gt=0)
    night_poll_multiplier: float = Field(default=2.0, ge=1)
    night_start_ist: time = time(0, 0)
    night_end_ist: time = time(7, 0)

    attachments_enabled: bool = True
    attachments_poll_s: float = Field(default=60.0, gt=0)
    attachments_batch: int = Field(default=20, ge=1)
    attachments_max_attempts: int = Field(default=5, ge=1)
    # Storage policy, not a research filter: every attachment URL is kept, so
    # anything skipped can be fetched later. Downloading every PDF costs
    # gigabytes per day (annual reports, presentations).
    attachments_max_mb: float = Field(default=5.0, gt=0)
    # Case-insensitive substrings of category/subcategory/subject. Env vars
    # take JSON arrays, e.g. GATS_ATTACHMENTS_INCLUDE='["order","result"]'.
    # An empty include list means "everything not excluded".
    attachments_include: list[str] = Field(
        default_factory=lambda: [
            "order",
            "contract",
            "result",
            "outcome of board meeting",
            "rating",
            "acquisition",
            "merger",
            "amalgamation",
            "scheme of arrangement",
            "buy back",
            "buyback",
            "bonus",
            "split",
            "dividend",
            "fund raising",
            "preferential",
            "qualified institution",
            "rights issue",
            "pledge",
            "press release",
            "joint venture",
            "agreement",
            "capacity",
            "commercial production",
            "commercial operation",
            "insolvency",
            "default",
            "fraud",
            "penalty",
            "litigation",
            "guidance",
        ]
    )
    attachments_exclude: list[str] = Field(
        default_factory=lambda: [
            "trading window",
            "newspaper",
            "postal ballot",
            "scrutinizer",
            "voting result",
            "74(5)",
            "share certificate",
            "duplicate",
            "investor complaint",
            "compliance certificate",
        ]
    )

    # Regular equity session, IST. Source: NSE "Market Timings" page
    # (nseindia.com/static/market-data/market-timings, checked 2026-10-03):
    # normal market 09:15-15:30; pre-open 09:00-09:08; closing session 15:40-16:00.
    session_open_ist: time = time(9, 15)
    session_close_ist: time = time(15, 30)

    eod_enabled: bool = True
    eod_check_s: float = Field(default=1800.0, gt=0)
    eod_catchup_days: int = Field(default=10, ge=1)
    eod_publish_after_ist: time = time(18, 0)
    eod_max_missing_attempts: int = Field(default=3, ge=1)
    indices_enabled: bool = True  # NSE index closes, same schedule as EOD
    # Corporate actions: refetched daily for a window around today, because
    # NSE announces actions weeks before their ex-date and revises them.
    corp_actions_enabled: bool = True
    corp_actions_lookback_days: int = Field(default=30, ge=1)
    corp_actions_lookahead_days: int = Field(default=90, ge=0)

    snapshots_enabled: bool = True
    refdata_enabled: bool = True  # BSE scrip list (and later reference files)
    # The security master is rebuilt once a day after the reference snapshots
    # (taken from daily_snapshot_after_ist), and new filings are linked to it.
    master_build_after_ist: time = time(9, 0)
    link_poll_s: float = Field(default=60.0, gt=0)
    extract_poll_s: float = Field(default=300.0, gt=0)  # attachment text extraction
    # Local LLM (Ollama, verified 2026-10-03: POST /api/chat with a JSON-schema
    # "format"). The model name must exist in `ollama list`.
    ollama_url: str = "http://localhost:11434"
    llm_model: str = "qwen2.5-coder:7b-instruct-q4_K_M"
    llm_timeout_s: float = Field(default=300.0, gt=0)
    llm_max_chars: int = Field(default=8000, ge=1000)  # facts sit in the first pages
    # Upstox market data (M5). The instrument file needs no login (verified
    # 2026-10-03). Candles need the read-only Analytics Token (valid a year,
    # generated at account.upstox.com/developer/apps#analytics): a secret,
    # set in .env only, never printed (SecretStr).
    upstox_api_base: str = "https://api.upstox.com"
    upstox_instruments_url: str = (
        "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
    )
    upstox_analytics_token: SecretStr | None = None
    # Event taxonomy (versioned YAML, relative to the working directory).
    taxonomy_path: Path = Path("configs/event_taxonomy.yaml")
    classify_poll_s: float = Field(default=120.0, gt=0)
    # Cross-exchange grouping of recent filings (rules: gats.refdata.dedupe).
    dedupe_poll_s: float = Field(default=300.0, gt=0)
    dedupe_lookback_days: int = Field(default=2, ge=1)
    snapshot_check_s: float = Field(default=1800.0, gt=0)
    daily_snapshot_after_ist: time = time(8, 0)

    # Reconcile: re-collect recent days in full so gaps (PC off, throttling,
    # >1 page of filings between polls) heal automatically.
    reconcile_enabled: bool = True
    reconcile_check_s: float = Field(default=3600.0, gt=0)
    reconcile_days: int = Field(default=7, ge=1)
    reconcile_max_attempts: int = Field(default=3, ge=1)
    backfill_max_pages: int = Field(default=1000, ge=1)

    job_error_backoff_max_s: float = Field(default=900.0, gt=0)

    # Health thresholds for `gats status` / `gats doctor`.
    status_heartbeat_stale_min: float = Field(default=15.0, gt=0)
    status_job_failing_min: float = Field(default=30.0, gt=0)
    status_bse_failures_warn: int = Field(default=10, ge=1)  # failed BSE fetches per hour
    status_data_warn_gb: float = Field(default=50.0, gt=0)
    status_disk_free_warn_gb: float = Field(default=5.0, ge=0)
    status_reconcile_queue_warn: int = Field(default=4, ge=0)

    @property
    def resolved_db_url(self) -> str:
        if self.db_url:
            return self.db_url
        return f"sqlite:///{(self.data_dir / 'gats.db').resolve().as_posix()}"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def bars_dir(self) -> Path:
        """Parquet bars (M5): too many rows for the database."""
        return self.data_dir / "bars"

    @property
    def heartbeat_path(self) -> Path:
        return self.data_dir / "heartbeat.json"

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.raw_dir, self.logs_dir):
            path.mkdir(parents=True, exist_ok=True)
