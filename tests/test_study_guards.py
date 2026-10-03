"""Guards that make a pre-registration binding (T3.3)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine
from typer.testing import CliRunner

from gats.cli import app
from gats.db import repo
from gats.rawstore import RawStore
from gats.refdata.calendar import TradingCalendar
from gats.research.runs import data_readiness, log_run, previous_runs
from gats.research.study import RegistrationError, registered_hash, verify_registration
from gats.sources.models import EodRecord
from gats.sources.nse_indices import IndexRecord
from tests.test_event_study import config

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs" / "studies" / "m3_event_study.yaml"
PREREG = ROOT / "docs" / "research" / "M3_prereg.md"


def test_committed_config_is_the_registered_one() -> None:
    """Fails in CI if anyone edits the M3 design after registration."""
    cfg, digest = verify_registration(CONFIG, PREREG)
    assert digest == registered_hash(PREREG)
    assert cfg.events.confirmatory == [
        "ORDER_WIN",
        "RATING_UP",
        "BUYBACK",
        "BONUS_SPLIT",
        "PRESS_RELEASE",
    ]
    assert cfg.data.test_start == date(2024, 1, 1) and cfg.costs.round_trip == 0.005


def test_edited_config_is_refused(tmp_path: Path) -> None:
    edited = tmp_path / "study.yaml"
    edited.write_text(
        CONFIG.read_text(encoding="utf-8").replace("0.005", "0.003"), encoding="utf-8"
    )
    with pytest.raises(RegistrationError, match="not the pre-registered config"):
        verify_registration(edited, PREREG)


def test_prereg_without_hash_is_refused(tmp_path: Path) -> None:
    doc = tmp_path / "prereg.md"
    doc.write_text("# no hash here\n", encoding="utf-8")
    with pytest.raises(RegistrationError, match="records no config"):
        verify_registration(CONFIG, doc)


def test_line_endings_do_not_change_the_hash(tmp_path: Path) -> None:
    crlf = tmp_path / "study.yaml"
    crlf.write_bytes(CONFIG.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    _, digest = verify_registration(crlf, PREREG)
    assert digest == registered_hash(PREREG)


def test_data_readiness(engine: Engine, store: RawStore) -> None:
    cfg = config()  # 2023-12-01 -> 2024-03-15
    sessions = [
        d
        for d in (date.fromordinal(date(2023, 12, 1).toordinal() + k) for k in range(106))
        if d.weekday() < 5
    ]
    cal = TradingCalendar(sessions=sessions)
    with engine.begin() as conn:
        empty = data_readiness(conn, cfg, cal)
        assert not empty.ready and len(empty.problems()) == 3
        doc = repo.save_raw(
            conn,
            store,
            b"x",
            kind="t",
            source="NSE",
            url="u",
            content_type=None,
            fetched_at=datetime(2024, 4, 1, tzinfo=UTC),
        )
        repo.upsert_eod(
            conn,
            [EodRecord(d, "X", "EQ", *([1.0] * 7), 1, 1.0, 1, 1, 1.0) for d in sessions],
            raw_doc_id=doc,
            parser_version="v",
            available_at=datetime(2024, 4, 1, tzinfo=UTC),
        )
        repo.upsert_index_eod(
            conn,
            [IndexRecord(d, "Nifty 500", 1, 1, 1, 1, 0, 0, 1, 1, 1, 1, 1) for d in sessions],
            raw_doc_id=doc,
            parser_version="v",
            available_at=datetime(2024, 4, 1, tzinfo=UTC),
        )
        day = cfg.data.start
        while day <= cfg.data.end:
            repo.record_backfill_day(
                conn,
                "NSE",
                day,
                complete=True,
                n_records=1,
                error=None,
                now=datetime(2024, 4, 1, tzinfo=UTC),
                max_attempts=3,
            )
            day = date.fromordinal(day.toordinal() + 1)
        full = data_readiness(conn, cfg, cal)
    assert full.ready, full.problems()


def test_run_log_flags_a_repeat(tmp_path: Path) -> None:
    assert previous_runs(tmp_path, "s", "abc") == []
    log_run(tmp_path, "s", {"run_id": "r1", "config_hash": "abc"})
    log_run(tmp_path, "s", {"run_id": "r2", "config_hash": "other"})
    assert [r["run_id"] for r in previous_runs(tmp_path, "s", "abc")] == ["r1"]


def test_cli_refuses_incomplete_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GATS_DATA_DIR", str(tmp_path / "data"))
    result = CliRunner().invoke(
        app, ["research", "event-study", "--config", str(CONFIG), "--prereg", str(PREREG)]
    )
    assert result.exit_code == 1
    assert "REFUSED: the data is not complete" in result.stdout
