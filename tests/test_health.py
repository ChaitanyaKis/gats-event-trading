"""Every health rule behind `gats status` / `gats doctor` (T1.5)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine
from typer.testing import CliRunner

from gats.cli import app
from gats.config import Settings
from gats.db import repo
from gats.health import Problem, evaluate
from gats.status import StatusReport, build_report

NOW = datetime(2026, 10, 5, 6, 0, tzinfo=UTC)  # Monday 11:30 IST


def ago(minutes: float) -> str:
    return (NOW - timedelta(minutes=minutes)).isoformat()


def healthy_report(**overrides: Any) -> StatusReport:
    report = StatusReport(
        heartbeat={
            "pid": 1,
            "jobs": {
                "bse_announcements": {"last_ok_at": ago(1), "consecutive_failures": 0},
                "nse_eod": {"last_ok_at": ago(20), "consecutive_failures": 0},
            },
        },
        data_bytes=1_000_000,
        disk_free_bytes=100 * 10**9,
    )
    for key, value in overrides.items():
        setattr(report, key, value)
    return report


def codes(problems: list[Problem]) -> list[str]:
    return [p.code for p in problems]


@pytest.fixture
def s(settings: Settings) -> Settings:
    return settings


def test_healthy_report_has_no_problems(s: Settings) -> None:
    assert evaluate(healthy_report(), NOW, s) == []


def test_no_heartbeat_means_recorder_never_ran(s: Settings) -> None:
    problems = evaluate(healthy_report(heartbeat=None), NOW, s)
    assert codes(problems) == ["recorder_never_ran"]
    assert "run_recorder.ps1" in problems[0].fix


def test_unreadable_heartbeat(s: Settings) -> None:
    problems = evaluate(healthy_report(heartbeat={"error": "unreadable heartbeat file"}), NOW, s)
    assert codes(problems) == ["heartbeat_unreadable"]


def test_stale_heartbeat(s: Settings) -> None:
    report = healthy_report()
    assert report.heartbeat is not None
    for info in report.heartbeat["jobs"].values():
        info["last_ok_at"] = ago(16)
    problems = evaluate(report, NOW, s)
    assert codes(problems) == ["heartbeat_stale"]
    assert "16 min ago" in problems[0].message


def test_recent_errors_keep_heartbeat_fresh(s: Settings) -> None:
    # A recorder that is running but failing is not "stopped".
    report = healthy_report(
        heartbeat={
            "jobs": {
                "bse_announcements": {
                    "last_ok_at": ago(60),
                    "last_error_at": ago(1),
                    "consecutive_failures": 9,
                    "last_error": "HTTP 503",
                }
            }
        }
    )
    assert codes(evaluate(report, NOW, s)) == ["job_failing"]


def test_job_failing_for_more_than_30_minutes(s: Settings) -> None:
    report = healthy_report()
    assert report.heartbeat is not None
    report.heartbeat["jobs"]["nse_eod"] = {
        "last_ok_at": ago(45),
        "last_error_at": ago(2),
        "consecutive_failures": 3,
        "last_error": "HTTP 403",
    }
    problems = evaluate(report, NOW, s)
    assert codes(problems) == ["job_failing"]
    assert "'nse_eod'" in problems[0].message and "45 min" in problems[0].message
    assert "HTTP 403" in problems[0].message


def test_short_failure_streak_is_not_flagged(s: Settings) -> None:
    report = healthy_report()
    assert report.heartbeat is not None
    report.heartbeat["jobs"]["nse_eod"] = {
        "last_ok_at": ago(10),
        "last_error_at": ago(1),
        "consecutive_failures": 2,
    }
    assert evaluate(report, NOW, s) == []


def test_job_that_never_succeeded(s: Settings) -> None:
    report = healthy_report()
    assert report.heartbeat is not None
    report.heartbeat["jobs"]["attachments"] = {
        "last_error_at": ago(1),
        "consecutive_failures": 1,
        "last_error": "boom",
    }
    problems = evaluate(report, NOW, s)
    assert codes(problems) == ["job_failing"]
    assert "since it started" in problems[0].message


def test_bse_failures(s: Settings) -> None:
    problems = evaluate(healthy_report(bse_failures_last_hour=12), NOW, s)
    assert codes(problems) == ["bse_failures"]
    assert "api.bseindia.com" in problems[0].fix


def test_large_data_dir(s: Settings) -> None:
    s.status_data_warn_gb = 1
    problems = evaluate(healthy_report(data_bytes=2 * 10**9), NOW, s)
    assert codes(problems) == ["data_large"]
    assert "Never delete" in problems[0].fix


def test_low_disk_is_a_failure(s: Settings) -> None:
    problems = evaluate(healthy_report(disk_free_bytes=10**9), NOW, s)
    assert [(p.code, p.level) for p in problems] == [("disk_low", "fail")]


def test_reconcile_backlog(s: Settings) -> None:
    assert evaluate(healthy_report(reconcile_queue=4), NOW, s) == []
    assert codes(evaluate(healthy_report(reconcile_queue=5), NOW, s)) == ["reconcile_backlog"]


def test_gave_up_days(s: Settings) -> None:
    problems = evaluate(healthy_report(gave_up_days=2, gave_up=["BSE 2026-09-28"]), NOW, s)
    assert codes(problems) == ["gave_up_days"]


def test_failures_are_listed_first(s: Settings) -> None:
    problems = evaluate(healthy_report(bse_failures_last_hour=50, disk_free_bytes=10**8), NOW, s)
    assert codes(problems) == ["disk_low", "bse_failures"]


class TestBuildReport:
    def test_counts_bse_failures_reconcile_queue_and_gave_up(
        self, engine: Engine, settings: Settings
    ) -> None:
        with engine.begin() as conn:
            for minutes, url, ok in [
                (10, "https://api.bseindia.com/x", False),
                (50, "https://api.bseindia.com/x", False),
                (90, "https://api.bseindia.com/x", False),  # older than an hour
                (10, "https://www.nseindia.com/x", False),  # not BSE
                (10, "https://api.bseindia.com/x", True),  # succeeded
            ]:
                repo.log_fetch(
                    conn, job="j", url=url, started_at=NOW - timedelta(minutes=minutes), ok=ok
                )
            for day, status in [(date(2026, 10, 4), "complete"), (date(2026, 10, 3), "gave_up")]:
                repo.record_backfill_day(
                    conn,
                    "BSE",
                    day,
                    complete=status == "complete",
                    n_records=1,
                    error=None,
                    now=NOW,
                    max_attempts=1,
                )
            report = build_report(conn, NOW, settings)
        assert report.bse_failures_last_hour == 2
        # 7 days x 2 sources, minus BSE 10-04 (complete) and BSE 10-03 (gave up).
        assert report.reconcile_queue == 12
        assert report.gave_up == ["BSE 2026-10-03"] and report.gave_up_days == 1
        assert report.disk_free_bytes is not None and report.disk_free_bytes > 0

    def test_heartbeat_is_read(self, engine: Engine, settings: Settings) -> None:
        settings.heartbeat_path.write_text(json.dumps({"pid": 7, "jobs": {}}), encoding="utf-8")
        with engine.begin() as conn:
            report = build_report(conn, NOW, settings)
        assert report.heartbeat == {"pid": 7, "jobs": {}}


class TestCli:
    runner = CliRunner()

    @pytest.fixture(autouse=True)
    def isolated(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("GATS_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setenv("GATS_RECONCILE_ENABLED", "false")
        return tmp_path

    def test_status_prints_health_first(self) -> None:
        result = self.runner.invoke(app, ["status"])
        assert result.exit_code == 0, result.stdout
        first = result.stdout.splitlines()[0]
        assert first.startswith("health:") and "1 problem" in first
        assert "No heartbeat file" in result.stdout

    def test_status_json_lists_problems(self) -> None:
        result = self.runner.invoke(app, ["status", "--json"])
        problems = json.loads(result.stdout)["problems"]
        assert [p["code"] for p in problems] == ["recorder_never_ran"]

    def test_doctor_fails_without_recorder(self) -> None:
        result = self.runner.invoke(app, ["doctor"])
        assert result.exit_code == 1
        assert "[FAIL] No heartbeat file" in result.stdout
        assert "[ok  ] database schema v" in result.stdout

    def test_doctor_passes_with_fresh_heartbeat(self, isolated: Path) -> None:
        data = isolated / "data"
        data.mkdir()
        fresh = datetime.now(UTC).isoformat()
        (data / "heartbeat.json").write_text(
            json.dumps({"jobs": {"bse": {"last_ok_at": fresh, "consecutive_failures": 0}}}),
            encoding="utf-8",
        )
        result = self.runner.invoke(app, ["doctor"])
        assert result.exit_code == 0, result.stdout
