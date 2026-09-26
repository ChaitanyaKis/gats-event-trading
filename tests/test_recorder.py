from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, time

import pytest

from gats.ingest import Outcome, Services
from gats.recorder import Heartbeat, _in_window, build_jobs, run_job_loop, run_recorder


@dataclass
class ScriptedJob:
    name: str
    script: list[Outcome | Exception]
    stop: asyncio.Event
    runs: int = 0
    seen: list[str] = field(default_factory=list)

    def interval_s(self, now: datetime) -> float:
        return 0.001

    async def run_once(self, svc: Services) -> Outcome:
        step = self.script[min(self.runs, len(self.script) - 1)]
        self.runs += 1
        if self.runs >= len(self.script):
            self.stop.set()
        if isinstance(step, Exception):
            raise step
        return step


async def test_job_crash_is_contained_and_recorded(svc: Services) -> None:
    stop = asyncio.Event()
    job = ScriptedJob(
        "flaky",
        [
            RuntimeError("boom"),
            Outcome(ok=False, error="HTTP 503"),
            Outcome(ok=True, n_new=3, n_records=5),
        ],
        stop,
    )
    heartbeat = Heartbeat(svc.settings.heartbeat_path)
    await asyncio.wait_for(run_job_loop(job, svc, stop, heartbeat, max_backoff_s=0.01), 5)

    assert job.runs == 3
    state = json.loads(svc.settings.heartbeat_path.read_text())["jobs"]["flaky"]
    assert state["consecutive_failures"] == 0
    assert state["last_new"] == 3
    assert "503" in state["last_error"]


async def test_recorder_stops_cleanly(svc: Services) -> None:
    stop = asyncio.Event()
    job = ScriptedJob("idle", [Outcome(ok=True)] * 3, stop)
    await asyncio.wait_for(run_recorder(svc, stop, jobs=[job]), 5)
    assert job.runs >= 3


async def test_recorder_requires_jobs(svc: Services) -> None:
    with pytest.raises(RuntimeError, match="no jobs"):
        await run_recorder(svc, asyncio.Event(), jobs=[])


def test_build_jobs_respects_flags(svc: Services) -> None:
    names = {job.name for job in build_jobs(svc)}
    assert names == {
        "bse_announcements",
        "nse_announcements",
        "nse_eod",
        "nse_bands",
        "nse_instruments",
        "attachments",
    }
    svc.settings.nse_enabled = False
    svc.settings.attachments_enabled = False
    names = {job.name for job in build_jobs(svc)}
    assert "nse_announcements" not in names and "attachments" not in names


@pytest.mark.parametrize(
    ("now", "start", "end", "inside"),
    [
        (time(1, 0), time(0, 0), time(7, 0), True),
        (time(8, 0), time(0, 0), time(7, 0), False),
        (time(23, 30), time(23, 0), time(6, 0), True),  # wraps midnight
        (time(3, 0), time(23, 0), time(6, 0), True),
        (time(12, 0), time(23, 0), time(6, 0), False),
    ],
)
def test_window(now: time, start: time, end: time, inside: bool) -> None:
    assert _in_window(now, start, end) is inside
