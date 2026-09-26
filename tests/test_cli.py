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
