"""A full paper-trading session against the mock exchange, offline (T7.1).

    .venv/Scripts/python scripts/paper_smoke.py

Everything runs in this one process, on a simulated clock, so a whole
session takes seconds: the mock exchange (a real HTTP server on 127.0.0.1),
the recorder's own NSE and hand-off jobs, and the paper runtime. Nothing
leaves the machine, nothing under ``data/`` is touched (it uses a fresh
temporary directory), and ``.env`` is not read.

What it proves end to end, over real HTTP and through the real code paths:

1. the recorder polls NSE, stores an order-win filing, and its hand-off
   types it, links it, downloads the attachment and reads its text;
2. the paper runtime extracts the order value, relates it to revenue, asks
   the broker for one-minute candles, and the configured strategy buys;
3. the position is held for the configured time, sold, and the day closed;
4. a second process could resume the run: the journal replays to the same
   account.

The market data is SYNTHETIC. The filing's attachment is a real order-win
PDF (``tests/fixtures/real/order_win_attachment.pdf``, Rs 60 crore).
Reference data the mock does not serve is seeded directly and says so
below: quarterly revenue and the start of surveillance history.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import sys
import tempfile
import threading
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from types import ModuleType

from pydantic import SecretStr

ROOT = Path(__file__).resolve().parents[1]
SESSION_DAY = date(2026, 10, 6)  # a Tuesday; the clock is simulated, so any weekday does
SYMBOL, ISIN = "MOCK1", "INE000001A01"
FILED_AT = time(10, 0, 10)
STEP_S = 15


def _load_mock() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "mock_exchange", ROOT / "scripts/mock_exchange.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Clock:
    """The session's simulated time."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(0)  # politeness waits cost nothing on a simulated clock


async def run(data_dir: Path, echo: bool = True) -> int:
    from sqlalchemy import select

    from gats import ingest
    from gats.config import Settings
    from gats.db import repo
    from gats.db.engine import init_db, make_engine
    from gats.db.schema import announcements, financial_results, surveillance_versions
    from gats.ingest import Services
    from gats.net import PoliteClient
    from gats.rawstore import RawStore
    from gats.recorder import HandOffJob, NseAnnouncementsJob, build_jobs
    from gats.refdata import master
    from gats.runtime.paper import PaperRuntime, load_system
    from gats.runtime.status import run_status
    from gats.timeutil import ist_datetime, to_ist

    def say(text: str) -> None:
        if echo:
            print(text)

    mock = _load_mock()
    clock = Clock(ist_datetime(SESSION_DAY, time(8, 0)))
    mock.CLOCK = clock
    mock.AUTO_ROWS = False
    mock.ATTACHMENT_PDF = (ROOT / "tests/fixtures/real/order_win_attachment.pdf").read_bytes()
    server = mock.serve(0)
    port = server.server_address[1]
    base = f"http://127.0.0.1:{port}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    filed = ist_datetime(SESSION_DAY, FILED_AT)
    mock.SCRIPTED_NSE.append((filed, mock.nse_order_win(filed, port, SYMBOL)))

    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        data_dir=data_dir,
        min_request_interval_s=0,
        host_min_interval_s={},
        max_retries=1,
        nse_home_url=f"{base}/nse/",
        nse_announcements_url=f"{base}/nse/api",
        nse_announcements_referer=f"{base}/nse/",
        nse_eod_url_template=base + "/eod/sec_bhavdata_full_{ddmmyyyy}.csv",
        nse_instruments_url=f"{base}/instruments.csv",
        nse_bands_url=f"{base}/bands.csv",
        upstox_api_base=f"{base}/upstox",
        upstox_analytics_token=SecretStr("smoke"),
        taxonomy_path=ROOT / "configs" / "event_taxonomy.yaml",
        bse_enabled=False,
    )
    settings.ensure_dirs()
    db = make_engine(settings.resolved_db_url)
    init_db(db)
    client = PoliteClient(
        user_agent="gats-smoke", min_interval_s=0, max_retries=1, sleep=clock.sleep
    )
    svc = Services(
        settings=settings, engine=db, store=RawStore(settings.raw_dir), client=client,
        clock=clock, sleep=clock.sleep,
    )  # fmt: skip
    try:
        # --- reference data: through the real ingestion, from the mock ---
        await ingest.ingest_snapshot(svc, "instruments", job="smoke")
        day, loaded = SESSION_DAY - timedelta(days=1), 0
        while loaded < 30:
            if day.weekday() < 5:
                outcome = await ingest.ingest_eod_day(svc, day, job="smoke", mode="backfill")
                loaded += outcome.ok
            day -= timedelta(days=1)
        with db.begin() as conn:
            master.build(conn, clock())
            # --- seeded directly (the mock serves neither): SYNTHETIC ---
            seed = repo.save_raw(
                conn, svc.store, b"paper_smoke seed", kind="smoke", source="SMOKE", url="seed",
                content_type=None, fetched_at=clock(),
            )  # fmt: skip
            for end in (
                date(2025, 9, 30),
                date(2025, 12, 31),
                date(2026, 3, 31),
                date(2026, 6, 30),
            ):
                shown = ist_datetime(end + timedelta(days=40), time(18, 0))
                conn.execute(
                    financial_results.insert().values(
                        symbol=SYMBOL,
                        period_end=end,
                        consolidated=True,
                        seq="1",
                        regime="legacy",
                        audited=False,
                        revised=False,
                        xbrl_status="done",
                        xbrl_attempts=1,
                        revenue=1e9,
                        event_ts=shown,
                        available_at=shown,
                        raw_doc_id=seed,
                        parser_version="smoke",
                    )
                )  # fmt: skip  (Rs 100 crore a quarter: the Rs 60 crore order is 15% of a year)
            conn.execute(
                surveillance_versions.insert().values(
                    entity_key="LTASM:OTHER",
                    valid_from=SESSION_DAY - timedelta(days=60),
                    list_name="LTASM",
                    symbol="OTHER",
                    available_at=datetime(2026, 8, 1, tzinfo=UTC),
                    raw_doc_id=seed,
                    parser_version="smoke",
                )
            )  # fmt: skip  (surveillance history exists, and MOCK1 is on no list)
        say(f"seeded: instruments, 30 sessions of prices, revenue, in {data_dir}")

        # --- the session ---
        jobs = build_jobs(svc)
        (nse_job,) = [j for j in jobs if isinstance(j, NseAnnouncementsJob)]
        (hand_off,) = [j for j in jobs if isinstance(j, HandOffJob)]
        loaded_system = load_system(Path("configs/paper.yaml"), settings, root=ROOT)
        system = replace(loaded_system, root=data_dir)  # its own kill switch, not data/KILL
        clock.now = ist_datetime(SESSION_DAY, time(9, 10))
        runtime = PaperRuntime(svc, system, "smoke")
        next_poll = clock.now
        end = ist_datetime(SESSION_DAY, time(15, 36))
        while clock.now <= end:
            at = f"{to_ist(clock.now):%H:%M:%S}"
            if clock.now >= next_poll:  # the recorder's NSE job, at its own interval
                polled = await nse_job.run_once(svc)
                next_poll = clock.now + timedelta(seconds=settings.nse_poll_s)
                if polled.n_new:
                    handed = await hand_off.run_once(svc)
                    say(f"{at} recorder: {polled.n_new} new filing, {handed.meta}")
            report = await runtime.tick()
            for order in report.orders:
                signal = order.signal
                say(f"{at} order: {signal.side} {order.quantity} {order.status} {order.note}")
            for fill in report.fills:
                say(f"{at} fill: {fill.side} {fill.quantity} @ {fill.price:.2f}")
            if report.closed:
                say(f"{at} day closed: {report.closed}")
            clock.now += timedelta(seconds=STEP_S)
        await runtime.aclose()

        # --- what happened, and would a restart agree? ---
        result = runtime.run.engine.result()
        resumed = PaperRuntime(svc, system, "smoke").run.engine.result()
        with db.begin() as conn:
            status = run_status(conn, "smoke")
            hand_over = conn.execute(select(announcements.c.available_at)).scalars().all()
        assert status is not None
        checks = {
            "one filing was recorded and handed over": status.steps["event"] == 1
            and len(hand_over) == 1,
            "the entry and the exit both filled": status.orders == {"filled": 2}
            and status.fills == 2,
            "the session ended flat": result.positions == {} and status.open_quantity == {},
            "the day was closed": status.steps["close"] == 1 and SESSION_DAY in result.equity,
            "a restart replays to the same account": resumed.cash == result.cash,
        }
        say(
            f"taken: {status.steps['event']} filing, {status.steps['bar']} bars; orders "
            f"{dict(status.orders)}; equity Rs {result.equity.get(SESSION_DAY, 0):,.2f}"
        )
        for name, ok in checks.items():
            say(f"  {'ok  ' if ok else 'FAIL'} {name}")
        return 0 if all(checks.values()) else 1
    finally:
        await client.aclose()
        db.dispose()
        server.shutdown()
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data-dir", type=Path, help="default: a fresh temporary directory")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    data_dir = args.data_dir or Path(tempfile.mkdtemp(prefix="gats-paper-smoke-"))
    if (data_dir / "gats.db").exists():
        print(f"{data_dir} already holds a database: give an empty directory")
        return 2
    code = asyncio.run(run(data_dir, echo=not args.quiet))
    if not args.quiet:
        print("PAPER SMOKE: " + ("PASS" if code == 0 else "FAIL"))
    return code


if __name__ == "__main__":
    sys.exit(main())
