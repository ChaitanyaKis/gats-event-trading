"""Command-line interface: ``gats --help``."""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
import sys
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import asdict, fields
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any

import typer
from sqlalchemy import select

from gats import __version__, ingest
from gats.config import Settings
from gats.db import repo
from gats.db.engine import init_db, make_engine
from gats.db.schema import announcements
from gats.ingest import Outcome, Services
from gats.logging_setup import configure_logging
from gats.net import FetchError, PoliteClient
from gats.rawstore import RawStore
from gats.recorder import run_recorder
from gats.sources import bse, nse, nse_archives
from gats.sources.models import PayloadError
from gats.status import build_report
from gats.timeutil import daterange, ist_datetime, ist_today, utcnow

app = typer.Typer(
    add_completion=False, no_args_is_help=True, help="GATS data recorder and research tools."
)
backfill_app = typer.Typer(no_args_is_help=True, help="Load historical data.")
app.add_typer(backfill_app, name="backfill")


class ProbeTarget(StrEnum):
    bse = "bse"
    nse = "nse"
    eod = "eod"
    bands = "bands"
    instruments = "instruments"


class AnnSource(StrEnum):
    bse = "bse"
    nse = "nse"


class ReparseKind(StrEnum):
    bse_ann = bse.KIND
    nse_ann = nse.KIND
    nse_eod = nse_archives.EOD_KIND
    nse_bands = nse_archives.BANDS_KIND
    nse_instruments = nse_archives.INSTRUMENTS_KIND


def _settings(log_to_file: bool = True) -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    configure_logging(settings.log_level, settings.logs_dir if log_to_file else None)
    return settings


@contextlib.asynccontextmanager
async def _services(settings: Settings) -> AsyncIterator[Services]:
    engine = make_engine(settings.resolved_db_url)
    init_db(engine)
    client = PoliteClient(
        user_agent=settings.user_agent,
        timeout_s=settings.http_timeout_s,
        min_interval_s=settings.min_request_interval_s,
        max_retries=settings.max_retries,
        backoff_base_s=settings.backoff_base_s,
        backoff_max_s=settings.backoff_max_s,
    )
    try:
        yield Services(
            settings=settings, engine=engine, store=RawStore(settings.raw_dir), client=client
        )
    finally:
        await client.aclose()
        engine.dispose()


def _parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(f"expected YYYY-MM-DD, got {value!r}") from exc


def _print_outcome(label: str, outcome: Outcome) -> None:
    state = "ok" if outcome.ok else f"FAILED ({outcome.error})"
    typer.echo(f"{label}: {state} records={outcome.n_records} new={outcome.n_new}")
    for warning in outcome.warnings[:5]:
        typer.echo(f"  warning: {warning}")


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


@app.command()
def init() -> None:
    """Create the data directory and database."""
    settings = _settings(log_to_file=False)
    engine = make_engine(settings.resolved_db_url)
    init_db(engine)
    engine.dispose()
    typer.echo(f"data dir: {settings.data_dir.resolve()}")
    typer.echo(f"database: {settings.resolved_db_url}")


@app.command()
def record() -> None:
    """Run the 24/7 recorder until Ctrl+C."""
    settings = _settings()

    async def main() -> None:
        stop = asyncio.Event()
        if sys.platform != "win32":  # Windows has no loop signal handlers; Ctrl+C still works
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, stop.set)
        async with _services(settings) as svc:
            await run_recorder(svc, stop)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        typer.echo("recorder stopped")


# --- probe ---------------------------------------------------------------------------


def _coverage(records: list[Any]) -> dict[str, str]:
    if not records:
        return {}
    counts: Counter[str] = Counter()
    for record in records:
        for f in fields(record):
            if getattr(record, f.name) is not None:
                counts[f.name] += 1
    return {f.name: f"{counts[f.name]}/{len(records)}" for f in fields(records[0])}


@app.command()
def probe(
    target: Annotated[ProbeTarget, typer.Argument(help="What to probe.")],
    day: Annotated[
        str | None,
        typer.Option("--date", help="YYYY-MM-DD (default: today IST, or last weekday for eod)."),
    ] = None,
) -> None:
    """Fetch one sample, save it, and report how the parser handles it.

    Run this for every target before the first `gats record`. Probe payloads
    are also written to data/probes/ so they can be inspected or shared.
    """
    settings = _settings(log_to_file=False)
    probes_dir = settings.data_dir / "probes"
    probes_dir.mkdir(parents=True, exist_ok=True)
    when = _parse_day(day) if day else None

    async def main() -> int:
        async with _services(settings) as svc:
            s = svc.settings
            the_day = when or ist_today()
            try:
                if target is ProbeTarget.bse:
                    got = await svc.client.get(
                        s.bse_announcements_url,
                        params=bse.request_params(the_day, the_day, 1),
                        headers=bse.request_headers(s.bse_referer),
                    )
                elif target is ProbeTarget.nse:
                    got = await svc.client.get(
                        s.nse_announcements_url,
                        params=nse.request_params(the_day, the_day),
                        headers=nse.request_headers(s.nse_announcements_referer),
                        warmup_url=s.nse_home_url,
                    )
                elif target is ProbeTarget.eod:
                    if when is None:
                        the_day = ist_today() - timedelta(days=1)
                        while the_day.weekday() >= 5:
                            the_day -= timedelta(days=1)
                    got = await svc.client.get(
                        nse_archives.eod_url(s.nse_eod_url_template, the_day),
                        warmup_url=s.nse_home_url,
                    )
                elif target is ProbeTarget.bands:
                    got = await svc.client.get(s.nse_bands_url, warmup_url=s.nse_home_url)
                else:
                    got = await svc.client.get(s.nse_instruments_url, warmup_url=s.nse_home_url)
            except FetchError as exc:
                typer.echo(f"FETCH FAILED: {exc}")
                return 1

            stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
            suffix = ".json" if target in (ProbeTarget.bse, ProbeTarget.nse) else ".csv"
            sample_path = probes_dir / f"{target.value}-{stamp}{suffix}"
            sample_path.write_bytes(got.content)
            with svc.engine.begin() as conn:
                repo.save_raw(
                    conn,
                    svc.store,
                    got.content,
                    kind=f"probe_{target.value}",
                    source="BSE" if target is ProbeTarget.bse else "NSE",
                    url=got.url,
                    content_type=got.content_type,
                    fetched_at=got.fetched_at,
                    meta={"date": the_day.isoformat()},
                )

            typer.echo(f"url:          {got.url}")
            typer.echo(f"http status:  {got.status}")
            typer.echo(f"content type: {got.content_type}")
            typer.echo(f"size:         {len(got.content):,} bytes  ({got.elapsed_ms} ms)")
            typer.echo(f"saved to:     {sample_path}")
            if not got.ok:
                typer.echo(f"first bytes:  {got.content[:300]!r}")
                return 1

            try:
                if target is ProbeTarget.bse:
                    parsed: Any = bse.parse_announcements(
                        got.content, attachment_base=s.bse_attachment_live_base
                    )
                elif target is ProbeTarget.nse:
                    parsed = nse.parse_announcements(got.content)
                elif target is ProbeTarget.eod:
                    parsed = nse_archives.parse_eod(got.content, trade_date=the_day)
                elif target is ProbeTarget.bands:
                    parsed = nse_archives.parse_bands(got.content)
                else:
                    parsed = nse_archives.parse_instruments(got.content)
            except PayloadError as exc:
                typer.echo(f"PARSE FAILED: {exc}")
                typer.echo(f"first bytes:  {got.content[:300]!r}")
                return 1

            typer.echo(f"records:      {len(parsed.records)}")
            typer.echo(f"meta:         {json.dumps(parsed.meta, default=str)}")
            typer.echo("field coverage (non-null / total):")
            for name, cov in _coverage(parsed.records).items():
                typer.echo(f"  {name:22} {cov}")
            for warning in parsed.warnings[:10]:
                typer.echo(f"warning: {warning}")
            if parsed.records:
                typer.echo("first record:")
                typer.echo(json.dumps(asdict(parsed.records[0]), indent=2, default=str))
            return 0

    raise typer.Exit(asyncio.run(main()))


# --- backfill ------------------------------------------------------------------------


def _day_already_backfilled(svc: Services, source: str, day: date) -> bool:
    start, end = (
        ist_datetime(day, datetime.min.time()),
        ist_datetime(day + timedelta(days=1), datetime.min.time()),
    )
    with svc.engine.begin() as conn:
        return (
            conn.execute(
                select(announcements.c.id)
                .where(
                    announcements.c.source == source,
                    announcements.c.ingest_mode == "backfill",
                    announcements.c.event_ts >= start,
                    announcements.c.event_ts < end,
                )
                .limit(1)
            ).first()
            is not None
        )


@backfill_app.command("announcements")
def backfill_announcements(
    source: Annotated[AnnSource, typer.Option(help="Exchange to backfill.")],
    start: Annotated[str, typer.Option(help="First day, YYYY-MM-DD.")],
    end: Annotated[str, typer.Option(help="Last day, YYYY-MM-DD.")],
    skip_existing: Annotated[bool, typer.Option(help="Skip days already backfilled.")] = True,
    max_pages: Annotated[int, typer.Option(help="BSE page cap per day.")] = 1000,
) -> None:
    """Load historical announcements day by day (resumable, idempotent)."""
    settings = _settings()
    first, last = _parse_day(start), _parse_day(end)

    async def main() -> None:
        async with _services(settings) as svc:
            for day in daterange(first, last):
                src = bse.SOURCE if source is AnnSource.bse else nse.SOURCE
                if skip_existing and _day_already_backfilled(svc, src, day):
                    typer.echo(f"{day}: already backfilled, skipping")
                    continue
                if source is AnnSource.bse:
                    outcome = await ingest.collect_bse(
                        svc,
                        day,
                        day,
                        job="backfill_bse",
                        mode="backfill",
                        max_pages=max_pages,
                        stop_when_no_new=False,
                    )
                else:
                    outcome = await ingest.collect_nse(
                        svc, day, day, job="backfill_nse", mode="backfill"
                    )
                _print_outcome(str(day), outcome)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        typer.echo("interrupted; rerun the same command to resume")


@backfill_app.command("eod")
def backfill_eod(
    start: Annotated[str, typer.Option(help="First day, YYYY-MM-DD.")],
    end: Annotated[str, typer.Option(help="Last day, YYYY-MM-DD.")],
) -> None:
    """Load historical end-of-day prices + delivery data (weekdays only)."""
    settings = _settings()
    first, last = _parse_day(start), _parse_day(end)

    async def main() -> None:
        async with _services(settings) as svc:
            for day in daterange(first, last):
                if day.weekday() >= 5:
                    continue
                with svc.engine.begin() as conn:
                    if repo.has_rows_for_date(
                        conn, ingest.snapshot_table_for("eod"), "trade_date", day
                    ):
                        typer.echo(f"{day}: already loaded")
                        continue
                outcome = await ingest.ingest_eod_day(svc, day, job="backfill_eod", mode="backfill")
                label = f"{day} (no file: holiday?)" if outcome.http_status == 404 else str(day)
                _print_outcome(label, outcome)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        typer.echo("interrupted; rerun the same command to resume")


# --- reparse & status ----------------------------------------------------------------


@app.command()
def reparse(kind: Annotated[ReparseKind, typer.Argument(help="Raw payload kind.")]) -> None:
    """Re-run the current parser over all stored raw payloads of KIND."""
    settings = _settings()

    async def main() -> dict[str, int]:
        async with _services(settings) as svc:
            return ingest.reparse_kind(svc, kind.value)

    stats = asyncio.run(main())
    typer.echo(json.dumps(stats))


@app.command()
def status(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Show what has been recorded and whether jobs are healthy."""
    settings = _settings(log_to_file=False)
    engine = make_engine(settings.resolved_db_url)
    init_db(engine)
    now = utcnow()
    with engine.begin() as conn:
        report = build_report(conn, now, settings.heartbeat_path)
    engine.dispose()
    if as_json:
        typer.echo(json.dumps(asdict(report), indent=2, default=str))
        return

    typer.echo(f"announcements: {report.announcements_by_source_mode or 'none'}")
    typer.echo(f"attachments:   {report.attachments_by_status or 'none'}")
    typer.echo(f"eod:           {report.eod_days} days, latest {report.eod_latest}")
    typer.echo(f"bands:         latest {report.bands_latest}")
    typer.echo(f"instruments:   latest {report.instruments_latest}")
    typer.echo(f"raw store:     {report.raw_docs} docs, {report.raw_bytes / 1e6:.1f} MB")
    typer.echo("live latency (s, dissemination -> stored, last 24h):")
    for src, summary in report.live_latency_s.items():
        typer.echo(f"  {src}: " + ", ".join(f"{k}={v:.1f}" for k, v in summary.items()))
    typer.echo("last fetch per job:")
    for job, info in sorted(report.last_fetch_by_job.items()):
        flag = "ok " if info["ok"] else "ERR"
        typer.echo(
            f"  [{flag}] {job:20} {info['at']}  http={info['http_status']}"
            + (f"  {info['error']}" if info["error"] else "")
        )
    if report.heartbeat:
        typer.echo(f"heartbeat pid {report.heartbeat.get('pid')}:")
        for job, info in sorted(report.heartbeat.get("jobs", {}).items()):
            typer.echo(
                f"  {job:20} last_ok={info.get('last_ok_at')} "
                f"failures={info.get('consecutive_failures', 0)}"
            )
