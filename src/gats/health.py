"""Health rules for ``gats status`` and ``gats doctor``.

Rules are pure functions of a :class:`~gats.status.StatusReport` and the
current time, so every warning is unit-testable without a recorder running.
Each problem carries a plain-language fix: the person reading it is usually
not the person who wrote the code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from gats.config import Settings
from gats.status import StatusReport

Level = Literal["warn", "fail"]

RECORDER_CMD = "powershell -ExecutionPolicy Bypass -File scripts\\run_recorder.ps1"


@dataclass(frozen=True, slots=True)
class Problem:
    level: Level
    code: str
    message: str
    fix: str


def _parse(ts: object) -> datetime | None:
    if not isinstance(ts, str):
        return None
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _minutes(delta: timedelta) -> int:
    return int(delta.total_seconds() // 60)


def _heartbeat_problems(report: StatusReport, now: datetime, s: Settings) -> list[Problem]:
    hb = report.heartbeat
    if hb is None:
        return [
            Problem(
                "fail",
                "recorder_never_ran",
                "No heartbeat file: the recorder has not run with this data directory.",
                f"Start it in its own terminal: {RECORDER_CMD}",
            )
        ]
    jobs = hb.get("jobs")
    if not isinstance(jobs, dict):
        return [
            Problem(
                "warn",
                "heartbeat_unreadable",
                "The heartbeat file could not be read.",
                "Restart the recorder; it rewrites the file on every job run.",
            )
        ]

    problems: list[Problem] = []
    stamps = [
        ts
        for info in jobs.values()
        if isinstance(info, dict)
        for ts in (_parse(info.get("last_ok_at")), _parse(info.get("last_error_at")))
        if ts is not None
    ]
    newest = max(stamps, default=None)
    stale_after = timedelta(minutes=s.status_heartbeat_stale_min)
    if newest is None or now - newest > stale_after:
        age = "never" if newest is None else f"{_minutes(now - newest)} min ago"
        problems.append(
            Problem(
                "fail",
                "heartbeat_stale",
                f"The recorder looks stopped: last job activity {age}.",
                f"Start it (and keep the PC awake): {RECORDER_CMD}",
            )
        )
        return problems  # per-job failures are moot when nothing is running

    failing_after = timedelta(minutes=s.status_job_failing_min)
    for name, info in sorted(jobs.items()):
        if not isinstance(info, dict) or not info.get("consecutive_failures"):
            continue
        last_ok = _parse(info.get("last_ok_at"))
        if last_ok is not None and now - last_ok <= failing_after:
            continue
        since = "since it started" if last_ok is None else f"for {_minutes(now - last_ok)} min"
        problems.append(
            Problem(
                "fail",
                "job_failing",
                f"Job '{name}' has been failing {since} "
                f"({info['consecutive_failures']} failures in a row): {info.get('last_error')}",
                "Run `gats inspect-bad` to see the failing requests and payloads. "
                "If an exchange changed its format, re-run `gats probe`.",
            )
        )
    return problems


def evaluate(report: StatusReport, now: datetime, settings: Settings) -> list[Problem]:
    """Every problem worth a human's attention, most severe first."""
    s = settings
    problems = _heartbeat_problems(report, now, s)

    if report.bse_failures_last_hour >= s.status_bse_failures_warn:
        problems.append(
            Problem(
                "warn",
                "bse_failures",
                f"{report.bse_failures_last_hour} BSE requests failed in the last hour "
                "(throttling or blocking).",
                "Check that only one BSE-heavy process runs. If a backfill is running "
                "next to the recorder, restart it with "
                "$env:GATS_HOST_MIN_INTERVAL_S='{\"api.bseindia.com\": 4}'.",
            )
        )

    data_gb = report.data_bytes / 1e9
    if data_gb > s.status_data_warn_gb:
        problems.append(
            Problem(
                "warn",
                "data_large",
                f"The data directory uses {data_gb:.1f} GB (limit {s.status_data_warn_gb:g} GB).",
                "Tighten the attachment policy (GATS_ATTACHMENTS_MAX_MB, "
                "GATS_ATTACHMENTS_INCLUDE) or raise GATS_STATUS_DATA_WARN_GB. "
                "Never delete files under data/.",
            )
        )
    if report.disk_free_bytes is not None:
        free_gb = report.disk_free_bytes / 1e9
        if free_gb < s.status_disk_free_warn_gb:
            problems.append(
                Problem(
                    "fail",
                    "disk_low",
                    f"Only {free_gb:.1f} GB free on the data disk.",
                    "Free up disk space or move GATS_DATA_DIR to a larger disk.",
                )
            )

    if report.reconcile_queue > s.status_reconcile_queue_warn:
        problems.append(
            Problem(
                "warn",
                "reconcile_backlog",
                f"{report.reconcile_queue} recent source-days are still incomplete.",
                "Keep the recorder running; the reconcile job retries them hourly. "
                "Days it gives up on are listed by `gats status --json` (gave_up).",
            )
        )
    if report.gave_up_days:
        problems.append(
            Problem(
                "warn",
                "gave_up_days",
                f"{report.gave_up_days} source-days were given up after repeated "
                "incomplete answers; nothing retries them automatically.",
                "Re-run `gats backfill announcements` for those dates (listed by "
                "`gats status --json` under gave_up); it refetches only incomplete days.",
            )
        )
    return sorted(problems, key=lambda p: p.level != "fail")
