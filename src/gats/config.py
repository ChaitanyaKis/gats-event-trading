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

from pydantic import Field
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
    nse_bands_url: str = "https://nsearchives.nseindia.com/content/equities/sec_list.csv"
    nse_instruments_url: str = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"

    # Recorder schedule
    bse_enabled: bool = True
    bse_poll_s: float = Field(default=30.0, gt=0)
    bse_max_pages: int = Field(default=10, ge=1)
    nse_enabled: bool = True
    nse_poll_s: float = Field(default=120.0, gt=0)
    night_poll_multiplier: float = Field(default=2.0, ge=1)
    night_start_ist: time = time(0, 0)
    night_end_ist: time = time(7, 0)

    attachments_enabled: bool = True
    attachments_poll_s: float = Field(default=60.0, gt=0)
    attachments_batch: int = Field(default=20, ge=1)
    attachments_max_attempts: int = Field(default=5, ge=1)

    eod_enabled: bool = True
    eod_check_s: float = Field(default=1800.0, gt=0)
    eod_catchup_days: int = Field(default=10, ge=1)
    eod_publish_after_ist: time = time(18, 0)
    eod_max_missing_attempts: int = Field(default=3, ge=1)

    snapshots_enabled: bool = True
    snapshot_check_s: float = Field(default=1800.0, gt=0)
    daily_snapshot_after_ist: time = time(8, 0)

    job_error_backoff_max_s: float = Field(default=900.0, gt=0)

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
    def heartbeat_path(self) -> Path:
        return self.data_dir / "heartbeat.json"

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.raw_dir, self.logs_dir):
            path.mkdir(parents=True, exist_ok=True)
