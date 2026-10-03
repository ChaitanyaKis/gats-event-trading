"""The experiment registry (T6.6): every research run, logged before it starts.

Multiple-testing corrections need the number of designs that were tried,
including the ones that failed or were abandoned. A log written after a run
would miss exactly those. So :func:`experiment` writes the row first (status
``running``), then lets the work run, then records the outcome; a crash
leaves a ``failed`` row behind and still counts.

A design is identified by ``params_hash`` (a pre-registered config's hash,
or a strategy's version with its cost, risk and engine settings). Running
the same design again is a reproduction, not a new trial, so
:func:`trial_count` counts distinct designs.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine, Row, func, select

from gats.db.schema import experiments
from gats.timeutil import utcnow


@dataclass
class Run:
    """Handed to the running experiment; it fills in ``metrics``."""

    id: int
    metrics: dict[str, Any] = field(default_factory=dict)


def git_state(root: Path) -> tuple[str | None, bool | None]:
    """(commit, uncommitted changes?) of the code that is running, or
    (None, None) outside a git checkout."""
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=root, capture_output=True, text=True, check=True,
        ).stdout.strip()  # fmt: skip
    except (OSError, subprocess.CalledProcessError):
        return None, None
    return sha or None, bool(dirty)


@contextmanager
def experiment(
    db: Engine,
    *,
    kind: str,
    name: str,
    params_hash: str,
    params: dict[str, Any],
    data_start: date | None,
    data_end: date | None,
    holdout: bool,
    clock: Callable[[], datetime] = utcnow,
    root: Path = Path(),
) -> Iterator[Run]:
    """Register a run, yield, then record how it ended. If the row cannot be
    written, the body never runs."""
    sha, dirty = git_state(root)
    with db.begin() as conn:
        key = conn.execute(
            experiments.insert().values(
                kind=kind,
                name=name,
                params_hash=params_hash,
                params=params,
                data_start=data_start,
                data_end=data_end,
                holdout=holdout,
                git_sha=sha,
                git_dirty=dirty,
                status="running",
                started_at=clock(),
            )
        ).inserted_primary_key
    if key is None:
        raise RuntimeError("the registry row was not created")
    run = Run(int(key[0]))
    try:
        yield run
    except BaseException as exc:
        _finish(db, run.id, "failed", run.metrics, f"{type(exc).__name__}: {exc}"[:2000], clock())
        raise
    _finish(db, run.id, "done", run.metrics, None, clock())


def _finish(
    db: Engine, run_id: int, status: str, metrics: dict[str, Any], error: str | None, at: datetime
) -> None:
    with db.begin() as conn:
        conn.execute(
            experiments.update()
            .where(experiments.c.id == run_id)
            .values(status=status, metrics=metrics or None, error=error, finished_at=at)
        )


def trial_count(conn: Connection, *, kind: str | None = None, name: str | None = None) -> int:
    """Distinct designs ever started (whatever became of them)."""
    query = select(func.count(func.distinct(experiments.c.params_hash)))
    if kind is not None:
        query = query.where(experiments.c.kind == kind)
    if name is not None:
        query = query.where(experiments.c.name == name)
    return int(conn.execute(query).scalar_one())


def recent(conn: Connection, *, kind: str | None = None, limit: int = 20) -> list[Row[Any]]:
    query = select(experiments).order_by(experiments.c.id.desc()).limit(limit)
    if kind is not None:
        query = query.where(experiments.c.kind == kind)
    return list(conn.execute(query).all())
