"""Several processes share one SQLite file (recorder, backfills, the paper
runtime) and SQLite has one writer at a time. On 2026-10-04 the recorder's
first run linked 156,000 filings in a single transaction and three
backfills gave up waiting for the lock. These tests pin the three fixes:
writers wait longer, batch jobs commit in small chunks, and a long job
tries a step again instead of ending.
"""

from __future__ import annotations

import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, event, select, text
from sqlalchemy.exc import OperationalError

from gats import cli
from gats.db import repo
from gats.db.engine import BUSY_TIMEOUT_MS, make_engine
from gats.db.schema import (
    announcement_event_types,
    announcement_security,
    document_texts,
    schema_meta,
)
from gats.extract.texts import extract_all
from gats.ingest import Services
from gats.recorder import ClassifyJob, ExtractJob, LinkJob, build_jobs
from gats.refdata import master
from gats.refdata.link import link_all
from gats.research.taxonomy import Taxonomy, classify_all
from gats.sources.models import InstrumentRecord
from gats.timeutil import to_ist
from tests.test_handoff import filing


def commits(engine: Engine) -> list[int]:
    """Counts the transactions committed on ``engine`` from now on."""
    seen: list[int] = []
    event.listen(engine, "commit", lambda conn: seen.append(1))
    return seen


def test_a_writer_waits_for_the_lock_instead_of_failing(engine: Engine, tmp_path: Path) -> None:
    assert BUSY_TIMEOUT_MS >= 60_000
    with engine.begin() as conn:
        assert conn.execute(text("PRAGMA busy_timeout")).scalar_one() == BUSY_TIMEOUT_MS
    other = make_engine(str(engine.url))  # another process, as far as SQLite can tell
    holding = threading.Event()

    def slow_writer() -> None:
        with other.connect() as conn:
            conn.exec_driver_sql("BEGIN IMMEDIATE")  # takes the one write lock
            conn.execute(schema_meta.insert().values(key="held", value="1"))
            holding.set()
            time.sleep(0.4)
            conn.exec_driver_sql("COMMIT")

    thread = threading.Thread(target=slow_writer)
    thread.start()
    assert holding.wait(5)
    started = time.monotonic()
    with engine.begin() as conn:  # must wait for the other writer, not raise
        conn.execute(schema_meta.insert().values(key="waited", value="1"))
    assert time.monotonic() - started > 0.2
    thread.join()
    other.dispose()
    with engine.begin() as conn:
        keys = set(conn.execute(select(schema_meta.c.key)).scalars())
    assert {"held", "waited"} <= keys


def test_classifying_and_linking_a_backlog_commit_in_small_chunks(svc: Services) -> None:
    now = svc.clock()
    with svc.engine.begin() as conn:
        page = repo.save_raw(
            conn, svc.store, b"list", kind="t", source="NSE", url="u", content_type=None,
            fetched_at=now,
        )  # fmt: skip
        repo.upsert_instruments(
            conn,
            to_ist(now).date(),
            [InstrumentRecord("AAA", "EQ", "INE00001A0101", "A Ltd", None, 10.0, 1)],
            raw_doc_id=page,
            parser_version="v",
            available_at=now - timedelta(days=1),
        )
        master.build(conn, now - timedelta(days=1))
    for n in range(1, 8):
        filing(svc, n, seen=now)
    taxonomy = Taxonomy.load(svc.settings.taxonomy_path)

    counted = commits(svc.engine)
    typed = classify_all(svc.engine, taxonomy, now, chunk=3)
    assert typed.classified == 7 and typed.by_type == {"ORDER_WIN": 7}
    assert len(counted) == 3  # 3 + 3 + 1: the lock is free between chunks
    assert classify_all(svc.engine, taxonomy, now, chunk=3).classified == 0

    counted.clear()
    linked = link_all(svc.engine, now, chunk=3)
    assert (linked.considered, linked.linked, linked.by_method) == (7, 7, {"nse_symbol": 7})
    assert len(counted) == 3
    assert link_all(svc.engine, now, chunk=3).considered == 0
    with svc.engine.begin() as conn:
        assert len(conn.execute(select(announcement_event_types)).all()) == 7
        assert len(conn.execute(select(announcement_security)).all()) == 7


async def test_the_recorders_batch_jobs_use_the_chunked_paths(svc: Services) -> None:
    now = svc.clock()
    for n in range(1, 4):
        filing(svc, n, seen=now)
    with svc.engine.begin() as conn:
        for n in range(3):  # three attachments nobody has read yet (not PDFs: errors are stored)
            repo.save_raw(
                conn, svc.store, f"not a pdf {n}".encode(), kind="attachment", source="NSE",
                url=f"u{n}", content_type="application/pdf", fetched_at=now,
            )  # fmt: skip
    jobs = {type(job): job for job in build_jobs(svc)}
    classified = await jobs[ClassifyJob].run_once(svc)
    assert classified.ok and classified.n_records == 3
    linked = await jobs[LinkJob].run_once(svc)
    assert linked.ok and linked.meta == {"skipped": "no security master yet"}

    counted = commits(svc.engine)
    read = extract_all(svc.engine, svc.store, now, chunk=2)
    assert (read.documents, read.errors) == (3, 3) and len(counted) == 2  # 2 + 1
    assert (await jobs[ExtractJob].run_once(svc)).n_records == 0  # nothing left for the job
    with svc.engine.begin() as conn:
        assert len(conn.execute(select(document_texts)).all()) == 3


async def test_a_long_job_tries_a_step_again_when_the_database_is_busy(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pauses: list[float] = []

    async def no_sleep(seconds: float) -> None:
        pauses.append(seconds)

    monkeypatch.setattr(cli.asyncio, "sleep", no_sleep)
    locked = OperationalError("INSERT ...", {}, Exception("database is locked"))
    attempts: list[int] = []

    async def step() -> str:
        attempts.append(1)
        if len(attempts) < 3:
            raise locked
        return "done"

    assert await cli._patiently(step, "2022-06-08") == "done"
    assert len(attempts) == 3 and pauses == [15, 30]
    assert "2022-06-08: the database is busy" in capsys.readouterr().out

    async def always() -> str:
        raise locked

    with pytest.raises(OperationalError):  # a database that stays busy is reported, not hidden
        await cli._patiently(always, "x")
    assert len(pauses) == 2 + (cli._LOCK_RETRIES - 1)

    async def broken() -> str:
        raise OperationalError("SELECT ...", {}, Exception("no such table: nope"))

    before = len(pauses)
    with pytest.raises(OperationalError, match="no such table"):
        await cli._patiently(broken, "x")
    assert len(pauses) == before  # only a busy database is waited out
