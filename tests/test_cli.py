from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gats import __version__
from gats.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)  # no stray .env is read
    monkeypatch.setenv("GATS_DATA_DIR", str(tmp_path / "data"))
    return tmp_path


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_init_creates_database(isolated_env: Path) -> None:
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.stdout
    assert (isolated_env / "data" / "gats.db").exists()


def test_status_on_empty_database() -> None:
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.stdout
    assert "announcements: none" in result.stdout


def test_status_json() -> None:
    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["eod_days"] == 0


def test_reparse_empty() -> None:
    result = runner.invoke(app, ["reparse", "bse_ann"])
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout.strip().splitlines()[-1])["documents"] == 0


def test_bad_date_is_rejected() -> None:
    result = runner.invoke(app, ["backfill", "eod", "--start", "25-09-2026", "--end", "2026-09-26"])
    assert result.exit_code != 0


def test_inspect_bad_on_empty_database() -> None:
    result = runner.invoke(app, ["inspect-bad"])
    assert result.exit_code == 0, result.stdout
    assert "recent failed fetches (0)" in result.stdout


def test_status_shows_backfill_line() -> None:
    result = runner.invoke(app, ["status"])
    assert "backfill days: none" in result.stdout


def test_redirected_windows_console_prints_rather_than_crashes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io
    import sys

    from gats.cli import _main

    raw = io.BytesIO()
    cp1252 = io.TextIOWrapper(raw, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", cp1252)
    _main()
    print("order of ₹72.77 crore")
    cp1252.flush()
    assert raw.getvalue().rstrip(b"\r\n") == b"order of ?72.77 crore"


def test_paper_status_without_runs() -> None:
    result = runner.invoke(app, ["paper", "status"])
    assert result.exit_code == 0, result.stdout
    assert "no paper runs yet" in result.stdout


def test_paper_run_refuses_without_its_config() -> None:
    result = runner.invoke(app, ["paper", "run", "--name", "s1"])  # no configs/ in this directory
    assert result.exit_code == 1
    assert "cannot load the paper system" in result.stdout


def test_probe_upstox_intraday_needs_a_known_symbol() -> None:
    result = runner.invoke(app, ["probe", "upstox-intraday", "--symbol", "NOSUCH"])
    assert result.exit_code == 1
    assert "no ISIN for NOSUCH" in result.stdout
