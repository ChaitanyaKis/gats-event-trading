"""Command-line interface: ``gats --help``."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import platform
import signal
import sys
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import asdict, fields
from datetime import date, time, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer
from sqlalchemy import func, select

from gats import __version__, ingest
from gats.config import Settings
from gats.db import repo
from gats.db.engine import SchemaVersionError, init_db, make_engine
from gats.db.schema import (
    SCHEMA_VERSION,
    announcements,
    fetch_log,
    raw_documents,
    securities,
    security_identifiers,
)
from gats.health import Problem, evaluate
from gats.ingest import Outcome, Services
from gats.logging_setup import configure_logging
from gats.net import FetchError, PoliteClient
from gats.pit import AsOf
from gats.rawstore import RawStore
from gats.recorder import run_recorder
from gats.refdata import dedupe, master
from gats.refdata.coverage import bse_scrip_isin_coverage, link_coverage
from gats.refdata.ingest import (
    ingest_bse_scrips,
    ingest_nse_corp_actions,
    ingest_nse_holidays,
    ingest_nse_surveillance,
    ingest_nse_symbol_changes,
)
from gats.refdata.link import link_pending
from gats.research.taxonomy import (
    CONTAINERS,
    NOISE,
    OTHER,
    Taxonomy,
    classify_pending,
    coverage,
    event_level_coverage,
)
from gats.sources import (
    bse,
    bse_scrips,
    nse,
    nse_archives,
    nse_corp_actions,
    nse_holidays,
    nse_indices,
    nse_surveillance,
    nse_symbols,
)
from gats.sources._util import preview
from gats.sources.models import PayloadError
from gats.status import build_report
from gats.timeutil import daterange, ist_datetime, ist_today, to_ist, utcnow

app = typer.Typer(
    add_completion=False, no_args_is_help=True, help="GATS data recorder and research tools."
)


@app.callback()
def _main() -> None:
    """GATS data recorder and research tools."""
    # Filing text holds characters (the rupee sign, smart quotes) that a
    # redirected Windows console (cp1252) cannot encode: print '?' instead
    # of crashing halfway through a command.
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(errors="replace")


backfill_app = typer.Typer(no_args_is_help=True, help="Load historical data.")
app.add_typer(backfill_app, name="backfill")
refdata_app = typer.Typer(no_args_is_help=True, help="Reference data: security master.")
app.add_typer(refdata_app, name="refdata")


class ProbeTarget(StrEnum):
    bse = "bse"
    nse = "nse"
    eod = "eod"
    indices = "indices"
    bands = "bands"
    instruments = "instruments"
    upstox_instruments = "upstox-instruments"
    upstox_candles = "upstox-candles"
    upstox_intraday = "upstox-intraday"
    upstox_charges = "upstox-charges"


class AnnSource(StrEnum):
    bse = "bse"
    nse = "nse"


class ReparseKind(StrEnum):
    bse_ann = bse.KIND
    nse_ann = nse.KIND
    nse_eod = nse_archives.EOD_KIND
    nse_bands = nse_archives.BANDS_KIND
    nse_instruments = nse_archives.INSTRUMENTS_KIND
    nse_indices = nse_indices.KIND
    bse_scrips = bse_scrips.KIND
    nse_symbol_changes = nse_symbols.KIND
    nse_holidays = nse_holidays.KIND
    nse_corp_actions = nse_corp_actions.KIND
    nse_asm = nse_surveillance.ASM_KIND
    nse_gsm = nse_surveillance.GSM_KIND


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
        host_min_interval_s=settings.host_min_interval_s,
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
    symbol: Annotated[str, typer.Option(help="NSE symbol, for upstox-candles.")] = "RELIANCE",
) -> None:
    """Fetch one sample, save it, and report how the parser handles it.

    Run this for every target before the first `gats record`. Probe payloads
    are also written to data/probes/ so they can be inspected or shared.
    """
    settings = _settings(log_to_file=False)
    probes_dir = settings.data_dir / "probes"
    probes_dir.mkdir(parents=True, exist_ok=True)
    when = _parse_day(day) if day else None
    if target is ProbeTarget.upstox_charges:
        raise typer.Exit(asyncio.run(_probe_upstox_charges(settings, symbol, probes_dir)))
    if target in (ProbeTarget.upstox_instruments, ProbeTarget.upstox_candles):
        raise typer.Exit(asyncio.run(_probe_upstox(settings, target, when, symbol, probes_dir)))
    if target is ProbeTarget.upstox_intraday:
        raise typer.Exit(asyncio.run(_probe_upstox_intraday(settings, symbol, probes_dir)))

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
                elif target in (ProbeTarget.eod, ProbeTarget.indices):
                    if when is None:
                        the_day = ist_today() - timedelta(days=1)
                        while the_day.weekday() >= 5:
                            the_day -= timedelta(days=1)
                    spec = ingest.EOD_FILE if target is ProbeTarget.eod else ingest.INDEX_FILE
                    got = await svc.client.get(spec.url(s, the_day), warmup_url=s.nse_home_url)
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
                elif target is ProbeTarget.indices:
                    parsed = nse_indices.parse_index_close(got.content, trade_date=the_day)
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


@backfill_app.command("announcements")
def backfill_announcements(
    source: Annotated[AnnSource, typer.Option(help="Exchange to backfill.")],
    start: Annotated[str, typer.Option(help="First day, YYYY-MM-DD.")],
    end: Annotated[str, typer.Option(help="Last day, YYYY-MM-DD.")],
    skip_complete: Annotated[
        bool, typer.Option(help="Skip days already backfilled completely.")
    ] = True,
    max_pages: Annotated[int | None, typer.Option(help="BSE page cap per day.")] = None,
) -> None:
    """Load historical announcements day by day (resumable, idempotent).

    A day counts as done only when every page was fetched; partial days are
    retried on the next run.
    """
    settings = _settings()
    first, last = _parse_day(start), _parse_day(end)
    src = bse.SOURCE if source is AnnSource.bse else nse.SOURCE
    pages = max_pages or settings.backfill_max_pages

    async def main() -> None:
        async with _services(settings) as svc:
            for day in daterange(first, last):
                if skip_complete:
                    with svc.engine.begin() as conn:
                        if repo.backfill_day_status(conn, src, day) == "complete":
                            typer.echo(f"{day}: complete, skipping")
                            continue
                outcome = await ingest.backfill_day(
                    svc, src, day, job=f"backfill_{source.value}", max_pages=pages
                )
                _print_outcome(f"{day} [{outcome.meta.get('backfill_status')}]", outcome)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        typer.echo("interrupted; rerun the same command to resume")


def _backfill_daily(
    spec: ingest.DailyFile, first: date, last: date, *, weekends: bool, job: str
) -> None:
    settings = _settings()

    async def main() -> None:
        async with _services(settings) as svc:
            today = ist_today()
            for day in daterange(first, last):
                if day.weekday() >= 5 and not weekends:
                    continue
                with svc.engine.begin() as conn:
                    if repo.has_rows_for_date(
                        conn, spec.rows_table, "trade_date", day
                    ) or repo.eod_day_settled(
                        repo.eod_day(conn, day, spec.days_table),
                        today=today,
                        max_attempts=settings.eod_max_missing_attempts,
                    ):
                        continue
                outcome = await ingest.ingest_daily_file(svc, spec, day, job=job, mode="backfill")
                if outcome.http_status == 404:
                    label = f"{day} (no file: weekend, holiday or not published)"
                elif outcome.ok and outcome.n_records == 0:
                    label = f"{day} (no session: the file holds another day)"
                else:
                    label = str(day)
                _print_outcome(label, outcome)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        typer.echo("interrupted; rerun the same command to resume")


@backfill_app.command("eod")
def backfill_eod(
    start: Annotated[str, typer.Option(help="First day, YYYY-MM-DD.")],
    end: Annotated[str, typer.Option(help="Last day, YYYY-MM-DD.")],
    weekends: Annotated[
        bool, typer.Option(help="Also ask for weekends (special sessions publish files).")
    ] = True,
) -> None:
    """Load historical end-of-day prices + delivery data (resumable).

    Days already loaded, or already known to have had no session, are skipped.
    """
    _backfill_daily(
        ingest.EOD_FILE, _parse_day(start), _parse_day(end), weekends=weekends, job="backfill_eod"
    )


@backfill_app.command("corporate-actions")
def backfill_corporate_actions(
    start: Annotated[str, typer.Option(help="First ex-date, YYYY-MM-DD.")],
    end: Annotated[str, typer.Option(help="Last ex-date, YYYY-MM-DD.")],
) -> None:
    """Load NSE corporate actions, one request per year of ex-dates (idempotent)."""
    settings = _settings()
    first, last = _parse_day(start), _parse_day(end)

    async def main() -> None:
        async with _services(settings) as svc:
            chunk_start = first
            while chunk_start <= last:
                chunk_end = min(
                    last, date(chunk_start.year + 1, chunk_start.month, 1) - timedelta(days=1)
                )
                outcome = await ingest_nse_corp_actions(
                    svc, chunk_start, chunk_end, job="backfill_corp_actions"
                )
                _print_outcome(f"{chunk_start}..{chunk_end}", outcome)
                chunk_start = chunk_end + timedelta(days=1)

    asyncio.run(main())


@backfill_app.command("results")
def backfill_results(
    event_type: Annotated[
        str, typer.Option("--type", help="Companies with filings of this type.")
    ] = "ORDER_WIN",
    since: Annotated[
        str, typer.Option(help="Read revenue for quarters ending on or after this day.")
    ] = "2021-01-01",
    limit: Annotated[int, typer.Option(help="Most companies and most XBRL files this run.")] = 500,
    refresh: Annotated[
        bool, typer.Option(help="Ask again about companies already stored.")
    ] = False,
) -> None:
    """Load quarterly results and their revenue for the companies behind an
    event type (resumable: rerun to continue)."""
    from gats.db.schema import announcement_event_types, announcement_security, financial_results
    from gats.refdata.results import fetch_pending_xbrl, ingest_results_index

    settings = _settings()
    tax = _taxonomy(settings)

    async def main() -> None:
        async with _services(settings) as svc:
            with svc.engine.begin() as conn:
                resolver = AsOf(conn, utcnow()).resolver()
                securities = conn.execute(
                    select(announcement_security.c.security_id)
                    .join(
                        announcement_event_types,
                        announcement_event_types.c.announcement_id
                        == announcement_security.c.announcement_id,
                    )
                    .where(
                        announcement_event_types.c.taxonomy_version == tax.version,
                        announcement_event_types.c.event_type == event_type,
                    )
                    .distinct()
                ).scalars()
                today = ist_today()
                symbols = sorted(
                    {
                        s
                        for sid in securities
                        if (s := resolver.identifier(sid, "nse_symbol", today))
                    }
                )
                stored = set(conn.execute(select(financial_results.c.symbol).distinct()).scalars())
            todo = [s for s in symbols if refresh or s not in stored][:limit]
            typer.echo(
                f"{len(symbols)} companies with {event_type} filings; asking about {len(todo)}"
            )
            for symbol in todo:
                outcome = await ingest_results_index(svc, symbol, job="backfill_results")
                _print_outcome(symbol, outcome)
            stats = await fetch_pending_xbrl(
                svc, since=_parse_day(since), limit=limit, job="backfill_results"
            )
            typer.echo(f"XBRL: {stats.attempted} read, {stats.done} done, {stats.failed} failed")

    asyncio.run(main())


@backfill_app.command("indices")
def backfill_indices(
    start: Annotated[str, typer.Option(help="First day, YYYY-MM-DD.")],
    end: Annotated[str, typer.Option(help="Last day, YYYY-MM-DD.")],
    weekends: Annotated[
        bool, typer.Option(help="Also ask for weekends (special sessions publish files).")
    ] = True,
) -> None:
    """Load historical NSE index closes (resumable)."""
    _backfill_daily(
        ingest.INDEX_FILE,
        _parse_day(start),
        _parse_day(end),
        weekends=weekends,
        job="backfill_indices",
    )


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


@app.command("inspect-bad")
def inspect_bad(
    limit: Annotated[int, typer.Option(help="How many of each to show.")] = 5,
) -> None:
    """Show recent failed fetches and the payloads that failed to parse."""
    settings = _settings(log_to_file=False)
    engine = make_engine(settings.resolved_db_url)
    init_db(engine)
    store = RawStore(settings.raw_dir)
    with engine.begin() as conn:
        failures = conn.execute(
            select(fetch_log)
            .where(fetch_log.c.ok.is_(False))
            .order_by(fetch_log.c.id.desc())
            .limit(limit)
        ).all()
        bad_docs = conn.execute(
            select(raw_documents)
            .where(raw_documents.c.kind.like("bad_%"))
            .order_by(raw_documents.c.first_fetched_at.desc())
            .limit(limit)
        ).all()
    engine.dispose()

    typer.echo(f"recent failed fetches ({len(failures)}):")
    for row in failures:
        typer.echo(
            f"  {row.started_at:%Y-%m-%d %H:%M:%S}Z {row.job:18} http={row.http_status} {row.error}"
        )
        typer.echo(f"      {row.url}")
    typer.echo(f"payloads that failed to parse ({len(bad_docs)}, identical payloads stored once):")
    for doc in bad_docs:
        body = store.get(doc.doc_id)
        typer.echo(f"  {doc.first_fetched_at:%Y-%m-%d %H:%M:%S}Z {doc.kind} {len(body)} bytes")
        typer.echo(f"      url:   {doc.url}")
        typer.echo(f"      error: {(doc.meta or {}).get('error')}")
        typer.echo(f"      body:  {preview(body, 300)}")


def _print_problems(problems: list[Problem]) -> None:
    if not problems:
        typer.echo("health:        no problems found")
        return
    typer.echo(f"health:        {len(problems)} problem(s)")
    for problem in problems:
        typer.echo(f"  [{problem.level.upper()}] {problem.message}")
        typer.echo(f"         fix: {problem.fix}")


@app.command()
def status(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Show what has been recorded and whether jobs are healthy."""
    settings = _settings(log_to_file=False)
    engine = make_engine(settings.resolved_db_url)
    init_db(engine)
    now = utcnow()
    with engine.begin() as conn:
        report = build_report(conn, now, settings)
    engine.dispose()
    problems = evaluate(report, now, settings)
    if as_json:
        payload = {**asdict(report), "problems": [asdict(p) for p in problems]}
        typer.echo(json.dumps(payload, indent=2, default=str))
        return

    _print_problems(problems)
    typer.echo(f"announcements: {report.announcements_by_source_mode or 'none'}")
    typer.echo(f"attachments:   {report.attachments_by_status or 'none'}")
    typer.echo(f"eod:           {report.eod_days} days, latest {report.eod_latest}")
    typer.echo(f"bands:         latest {report.bands_latest}")
    typer.echo(f"instruments:   latest {report.instruments_latest}")
    typer.echo(f"backfill days: {report.backfill_days or 'none'}")
    typer.echo(f"reconcile:     {report.reconcile_queue} recent source-days still to fetch")
    typer.echo(f"raw store:     {report.raw_docs} docs, {report.raw_bytes / 1e6:.1f} MB")
    free_gb = report.disk_free_bytes / 1e9 if report.disk_free_bytes is not None else None
    typer.echo(
        f"disk:          ~{report.data_bytes / 1e9:.2f} GB used"
        + (f", {free_gb:.1f} GB free" if free_gb is not None else "")
    )
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


async def _network_checks(svc: Services) -> list[tuple[str, bool, str]]:
    """One light request per source. The answer is parsed, so a format change
    shows up as a failure rather than a reassuring HTTP 200."""
    s = svc.settings
    today = ist_today()
    results: list[tuple[str, bool, str]] = []

    async def check(name: str, url: str, parse: Any, **kwargs: Any) -> None:
        try:
            got = await svc.client.get(url, **kwargs)
        except FetchError as exc:
            results.append((name, False, str(exc)))
            return
        if not got.ok:
            results.append((name, False, f"HTTP {got.status}"))
            return
        try:
            n = len(parse(got.content).records)
        except PayloadError as exc:
            results.append((name, False, f"parse failed: {exc}"))
            return
        results.append((name, True, f"HTTP {got.status}, {n} records"))

    await check(
        "BSE announcements",
        s.bse_announcements_url,
        lambda b: bse.parse_announcements(b, attachment_base=s.bse_attachment_live_base),
        params=bse.request_params(today, today, 1),
        headers=bse.request_headers(s.bse_referer),
        warmup_url=s.bse_referer if s.bse_warmup else None,
    )
    await check(
        "NSE announcements",
        s.nse_announcements_url,
        nse.parse_announcements,
        params=nse.request_params(today, today),
        headers=nse.request_headers(s.nse_announcements_referer),
        warmup_url=s.nse_home_url,
    )
    await check(
        "NSE price bands", s.nse_bands_url, nse_archives.parse_bands, warmup_url=s.nse_home_url
    )
    await check(
        "NSE instruments",
        s.nse_instruments_url,
        nse_archives.parse_instruments,
        warmup_url=s.nse_home_url,
    )
    return results


@app.command()
def doctor(
    network: Annotated[
        bool, typer.Option(help="Also make one request per exchange source.")
    ] = False,
) -> None:
    """Run health checks and print how to fix each problem (exit code 1 on failure)."""
    settings = _settings(log_to_file=False)
    failed = False

    def line(ok: bool, what: str, fix: str | None = None) -> None:
        typer.echo(f"[{'ok' if ok else 'FAIL':4}] {what}")
        if fix:
            typer.echo(f"       fix: {fix}")

    py_ok = sys.version_info >= (3, 11)
    failed |= not py_ok
    line(
        py_ok,
        f"python {platform.python_version()} on {platform.system()}",
        None if py_ok else "Install Python 3.11+ and recreate .venv.",
    )

    data_dir = settings.data_dir.resolve()
    writable = data_dir.exists() and os.access(data_dir, os.W_OK)
    failed |= not writable
    line(
        writable,
        f"data dir {data_dir}",
        None if writable else "Run `gats init`, or point GATS_DATA_DIR at a writable folder.",
    )

    engine = make_engine(settings.resolved_db_url)
    try:
        init_db(engine)
    except SchemaVersionError as exc:
        engine.dispose()
        line(False, f"database: {exc}", "Update GATS: git pull, then pip install -e .[dev]")
        raise typer.Exit(1) from exc
    now = utcnow()
    with engine.begin() as conn:
        report = build_report(conn, now, settings)
    engine.dispose()
    line(True, f"database schema v{SCHEMA_VERSION}")

    for problem in evaluate(report, now, settings):
        failed |= problem.level == "fail"
        typer.echo(f"[{'FAIL' if problem.level == 'fail' else 'warn':4}] {problem.message}")
        typer.echo(f"       fix: {problem.fix}")

    if network:

        async def run() -> list[tuple[str, bool, str]]:
            async with _services(settings) as svc:
                return await _network_checks(svc)

        for name, ok, detail in asyncio.run(run()):
            failed |= not ok
            line(
                ok,
                f"{name}: {detail}",
                None if ok else "Run `gats probe` for this source; see docs/DATA_SOURCES.md.",
            )
    raise typer.Exit(1 if failed else 0)


# --- refdata ---------------------------------------------------------------------------


@refdata_app.command("update")
def refdata_update() -> None:
    """Take today's snapshot of every reference file now: BSE scrips, NSE
    symbol changes, NSE instruments and price bands (the recorder does this
    daily after 08:00 IST)."""
    settings = _settings()

    async def main() -> list[tuple[str, Outcome]]:
        async with _services(settings) as svc:
            job = "refdata_update"
            results = [
                ("bse_scrips", await ingest_bse_scrips(svc, job=job)),
                ("nse_symbol_changes", await ingest_nse_symbol_changes(svc, job=job)),
                ("nse_holidays", await ingest_nse_holidays(svc, job=job)),
            ]
            asm, gsm = await ingest_nse_surveillance(svc, job=job)
            results += [("nse_asm", asm), ("nse_gsm", gsm)]
            for what in ("instruments", "bands"):
                with svc.engine.begin() as conn:
                    have = repo.has_rows_for_date(
                        conn, ingest.snapshot_table_for(what), "as_of_date", ist_today()
                    )
                if not have:
                    results.append(
                        (f"nse_{what}", await ingest.ingest_snapshot(svc, what, job=job))
                    )
            return results

    results = asyncio.run(main())
    for label, outcome in results:
        _print_outcome(label, outcome)
        typer.echo(f"  {outcome.meta}")
    raise typer.Exit(0 if all(o.ok for _, o in results) else 1)


@refdata_app.command("coverage")
def refdata_coverage(
    days: Annotated[int, typer.Option(help="Look back this many days.")] = 30,
    top: Annotated[int, typer.Option(help="Unresolved identifiers to list.")] = 15,
) -> None:
    """How many recent filings resolve to a security."""
    settings = _settings(log_to_file=False)
    engine = make_engine(settings.resolved_db_url)
    init_db(engine)
    since = utcnow() - timedelta(days=days)
    with engine.begin() as conn:
        reports = [
            link_coverage(conn, "NSE", since, top),
            link_coverage(conn, "BSE", since, top),
            bse_scrip_isin_coverage(conn, since, top),
        ]
    engine.dispose()
    for cov in reports:
        typer.echo(
            f"{cov.label} (last {days} days): filings {cov.resolved_filings}/{cov.n_filings} "
            f"= {cov.filing_share:.1%}, ids {cov.resolved_ids}/{cov.n_ids} = {cov.id_share:.1%}"
        )
        for ident, name, n in cov.unresolved:
            typer.echo(f"  unresolved {ident:>10}  {n:5} filings  {name}")


@refdata_app.command("build")
def refdata_build() -> None:
    """Rebuild the security master and link filings to it (idempotent)."""
    settings = _settings()
    engine = make_engine(settings.resolved_db_url)
    init_db(engine)
    with engine.begin() as conn:
        stats = master.build(conn, utcnow())
    typer.echo(f"build {stats.build_id}: {stats.as_dict()}")
    for conflict in stats.conflicts[:10]:
        typer.echo(f"  conflict: {conflict}")
    with engine.begin() as conn:
        linked = link_pending(conn, utcnow())
    engine.dispose()
    typer.echo(
        f"linked {linked.linked} filings, {linked.unresolved} unresolved "
        f"(of {linked.considered} needing a link) {linked.by_method}"
    )


class IdType(StrEnum):
    isin = "isin"
    nse_symbol = "nse_symbol"
    bse_scrip = "bse_scrip"


@refdata_app.command("resolve")
def refdata_resolve(
    id_type: Annotated[IdType, typer.Argument(help="Identifier type.")],
    value: Annotated[str, typer.Argument(help="Identifier value.")],
    on: Annotated[str | None, typer.Option("--date", help="YYYY-MM-DD (default today).")] = None,
) -> None:
    """Show which security an identifier meant on a date, with all its identifiers."""
    settings = _settings(log_to_file=False)
    engine = make_engine(settings.resolved_db_url)
    init_db(engine)
    day = _parse_day(on) if on else ist_today()
    with engine.begin() as conn:
        security_id = master.resolve(conn, id_type.value, value, day)
        if security_id is None:
            typer.echo(f"{id_type.value} {value} on {day}: unresolved (unknown or ambiguous)")
            raise typer.Exit(1)
        sec = conn.execute(select(securities).where(securities.c.security_id == security_id)).one()
        idents = conn.execute(
            select(security_identifiers)
            .where(
                security_identifiers.c.security_id == security_id,
                security_identifiers.c.last_build_id == master.latest_build_id(conn),
            )
            .order_by(security_identifiers.c.id_type, security_identifiers.c.valid_from)
        ).all()
    engine.dispose()
    typer.echo(f"security {security_id}: {sec.name} (ISIN {sec.primary_isin})")
    for ident in idents:
        start = "" if ident.valid_from == master.OPEN_START else str(ident.valid_from)
        typer.echo(
            f"  {ident.id_type:10} {ident.value:12} [{start or '...'}, {ident.valid_to or '...'})"
            f"  source={ident.source}"
        )


@refdata_app.command("dedupe")
def refdata_dedupe(
    start: Annotated[str | None, typer.Option(help="First day (default: earliest filing).")] = None,
    end: Annotated[str | None, typer.Option(help="Last day, inclusive (default: today).")] = None,
) -> None:
    """Group BSE/NSE filings of the same disclosure into events (idempotent)."""
    settings = _settings()
    engine = make_engine(settings.resolved_db_url)
    init_db(engine)
    with engine.begin() as conn:
        first_ts = conn.execute(select(func.min(announcements.c.event_ts))).scalar()
    if first_ts is None:
        typer.echo("no filings yet")
        engine.dispose()
        return
    first = _parse_day(start) if start else to_ist(first_ts).date()
    last = _parse_day(end) if end else ist_today()
    total = dedupe.DedupeStats()
    day = first
    while day <= last:  # a month per transaction keeps memory and locks small
        chunk_end = min(last, day + timedelta(days=30))
        with engine.begin() as conn:
            stats = dedupe.group_range(
                conn,
                ist_datetime(day, time()),
                ist_datetime(chunk_end + timedelta(days=1), time()),
                utcnow(),
            )
        total.filings += stats.filings
        total.pairs += stats.pairs
        total.singles += stats.singles
        day = chunk_end + timedelta(days=1)
    engine.dispose()
    typer.echo(
        f"{total.filings} filings: {total.pairs} cross-exchange pairs, "
        f"{total.singles} single-exchange events (rules {dedupe.RULES_VERSION})"
    )


# --- events ----------------------------------------------------------------------------

events_app = typer.Typer(no_args_is_help=True, help="Event typing (taxonomy).")
app.add_typer(events_app, name="events")


def _taxonomy(settings: Settings) -> Taxonomy:
    if not settings.taxonomy_path.exists():
        typer.echo(
            f"taxonomy not found: {settings.taxonomy_path.resolve()} (run from the repo root)"
        )
        raise typer.Exit(1)
    return Taxonomy.load(settings.taxonomy_path)


@events_app.command("classify")
def events_classify() -> None:
    """Type every filing not yet typed under the current taxonomy version."""
    settings = _settings()
    tax = _taxonomy(settings)
    engine = make_engine(settings.resolved_db_url)
    init_db(engine)
    with engine.begin() as conn:
        stats = classify_pending(conn, tax, utcnow())
    engine.dispose()
    typer.echo(
        f"{tax.version}: typed {stats.classified} filings {dict(stats.by_type.most_common())}"
    )


@events_app.command("coverage")
def events_coverage(
    top: Annotated[int, typer.Option(help="Unmapped category patterns to list.")] = 15,
) -> None:
    """Share of non-noise filings the taxonomy leaves as OTHER (target <= 10%)."""
    settings = _settings(log_to_file=False)
    tax = _taxonomy(settings)
    engine = make_engine(settings.resolved_db_url)
    init_db(engine)
    with engine.begin() as conn:
        filings = coverage(conn, tax.version, top=top)
        events = event_level_coverage(conn, tax.version)
    engine.dispose()
    containers = sum(events.by_type.get(c, 0) for c in sorted(CONTAINERS))
    non_noise = events.total - events.by_type.get(NOISE, 0)
    typer.echo(f"{tax.version}: {filings.total} filings typed")
    typer.echo(f"  OTHER, per filing:          {filings.other_share_excl_noise:.1%} of non-noise")
    typer.echo(f"  OTHER, typed via its event: {events.other_share_excl_noise:.1%} of non-noise")
    if non_noise:
        typer.echo(
            "  ... counting containers (BOARD_OUTCOME, PRESS_RELEASE) as OTHER: "
            f"{(events.by_type.get(OTHER, 0) + containers) / non_noise:.1%}"
        )
    for event_type, n in sorted(events.by_type.items(), key=lambda x: -x[1]):
        typer.echo(f"    {event_type:20} {n}")
    typer.echo("  top OTHER patterns (filing level):")
    for source, cat, sub, n in filings.unmapped:
        typer.echo(f"    {n:6}  {source} | {cat} | {sub}")


# --- research ---------------------------------------------------------------------------

research_app = typer.Typer(no_args_is_help=True, help="Pre-registered studies.")
app.add_typer(research_app, name="research")


@research_app.command("event-study")
def research_event_study(
    config: Annotated[Path, typer.Option(help="Study config (YAML).")] = Path(
        "configs/studies/m3_event_study.yaml"
    ),
    prereg: Annotated[
        Path, typer.Option(help="Pre-registration recording the config's hash.")
    ] = Path("docs/research/M3_prereg.md"),
) -> None:
    """Run the pre-registered event study on complete data (refuses otherwise)."""
    from gats.research.event_study import filter_counts, run_event_study, to_records, write_parquet
    from gats.research.registry import experiment
    from gats.research.runs import (
        data_readiness,
        database_fingerprint,
        log_run,
        previous_runs,
        stamp,
    )
    from gats.research.study import RegistrationError, verify_registration

    settings = _settings()
    try:
        cfg, digest = verify_registration(config, prereg)
    except RegistrationError as exc:
        typer.echo(f"REFUSED: {exc}")
        raise typer.Exit(1) from exc
    engine = make_engine(settings.resolved_db_url)
    init_db(engine)
    with engine.begin() as conn:
        clock = AsOf(conn, utcnow())
        readiness = data_readiness(conn, cfg, clock.calendar())
        if not readiness.ready:
            typer.echo("REFUSED: the data is not complete enough to spend the holdout on.")
            for problem in readiness.problems():
                typer.echo(f"  {problem}")
            typer.echo(f"  first missing price sessions: {readiness.missing_eod[:5]}")
            typer.echo(f"  first missing filing days: {readiness.missing_filing_days[:5]}")
            typer.echo("  Run the T3.0 backfill commands (docs/PROGRESS.md), then retry.")
            engine.dispose()
            raise typer.Exit(1)
        earlier = previous_runs(settings.data_dir, cfg.study, digest)
        if earlier:
            typer.echo(
                f"WARNING: this exact design was already run {len(earlier)} time(s) "
                f"(first {earlier[0]['run_id']}). This is a reproduction; nothing may be "
                "tuned on its test-period results."
            )
        registered = experiment(
            engine,
            kind="event_study",
            name=cfg.study,
            params_hash=digest,
            params={"config": config.as_posix(), "taxonomy_version": cfg.taxonomy_version},
            data_start=cfg.data.start,
            data_end=cfg.data.end,
            holdout=True,  # the study reads its test period
        )
        with registered as run:
            results = run_event_study(clock, cfg)
            run.metrics = {"events": len(results)}
        fingerprint = database_fingerprint(conn)
    engine.dispose()
    run_id = stamp()
    out = settings.data_dir / "research" / cfg.study / run_id
    rows = to_records(results, [x.name for x in cfg.exits])
    write_parquet(rows, out / "events.parquet")
    log_run(
        settings.data_dir,
        cfg.study,
        {"run_id": run_id, "config_hash": digest, "events": len(rows), **fingerprint},
    )
    typer.echo(f"run {run_id}: {len(rows)} events -> {out / 'events.parquet'}")
    for event_type, counts in sorted(filter_counts(results).items()):
        typer.echo(f"  {event_type:20} {counts}")
    _write_m3_report(rows, cfg, digest, run_id, fingerprint, log_trial=not earlier)


def _write_m3_report(
    rows: list[dict[str, Any]],
    cfg: Any,
    digest: str,
    run_id: str,
    fingerprint: dict[str, Any],
    *,
    log_trial: bool,
) -> None:
    from gats.research.report import build_report, car_plot, trial_row

    reports = Path("reports")
    figure = reports / "figures" / "m3_car.png"
    text, family = build_report(
        rows,
        cfg,
        config_hash=digest,
        run_id=run_id,
        fingerprint=fingerprint,
        figure="figures/m3_car.png",
    )
    from gats.research.report import cells_for

    cells, _ = cells_for(rows, cfg)
    car_plot(cells, cfg, figure)
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "M3_event_study.md").write_bytes(text.encode("utf-8"))
    typer.echo(f"report: {reports / 'M3_event_study.md'}")
    passing = [f"{c.event_type} {c.exit}" for c in family if c.passes]
    typer.echo("G1: " + ("PASS for " + ", ".join(passing) if passing else "no edge found"))
    trials = Path("docs/research/trials.md")
    if log_trial and trials.exists():
        with trials.open("a", encoding="utf-8") as handle:
            handle.write(trial_row(run_id, cfg, family, ist_today()) + "\n")
        typer.echo(f"trial logged in {trials}")


@research_app.command("report")
def research_report(
    run: Annotated[str, typer.Option(help="Run id (a folder under data/research/<study>/).")],
    config: Annotated[Path, typer.Option(help="Study config (YAML).")] = Path(
        "configs/studies/m3_event_study.yaml"
    ),
    prereg: Annotated[
        Path, typer.Option(help="Pre-registration recording the config's hash.")
    ] = Path("docs/research/M3_prereg.md"),
) -> None:
    """Rebuild the report from a saved run without recomputing anything."""
    import pyarrow.parquet as pq

    from gats.research.runs import previous_runs
    from gats.research.study import verify_registration

    settings = _settings(log_to_file=False)
    cfg, digest = verify_registration(config, prereg)
    path = settings.data_dir / "research" / cfg.study / run / "events.parquet"
    if not path.exists():
        typer.echo(f"no such run: {path}")
        raise typer.Exit(1)
    rows = pq.read_table(path).to_pylist()
    logged = [r for r in previous_runs(settings.data_dir, cfg.study, digest) if r["run_id"] == run]
    fingerprint = {k: v for k, v in (logged[0] if logged else {}).items() if k.startswith("eod")}
    _write_m3_report(rows, cfg, digest, run, fingerprint, log_trial=False)


# --- extract -----------------------------------------------------------------------------

extract_app = typer.Typer(no_args_is_help=True, help="Attachment text (M4).")
app.add_typer(extract_app, name="extract")


def _typed_ids(conn: Any, event_type: str, version: str, since: date | None) -> list[int]:
    from gats.db.schema import announcement_event_types

    query = (
        select(announcements.c.id)
        .join(
            announcement_event_types,
            announcement_event_types.c.announcement_id == announcements.c.id,
        )
        .where(
            announcement_event_types.c.taxonomy_version == version,
            announcement_event_types.c.event_type == event_type,
        )
        .order_by(announcements.c.event_ts.desc())
    )
    if since is not None:
        query = query.where(announcements.c.event_ts >= ist_datetime(since, time()))
    return [int(i) for i in conn.execute(query).scalars()]


@extract_app.command("fetch")
def extract_fetch(
    event_type: Annotated[str, typer.Option("--type", help="Event type, e.g. ORDER_WIN.")],
    limit: Annotated[int, typer.Option(help="Most downloads in this run (newest first).")] = 300,
    since: Annotated[str | None, typer.Option(help="Only filings from YYYY-MM-DD.")] = None,
) -> None:
    """Download the attachments of one event type, bypassing the storage policy."""
    settings = _settings()
    tax = _taxonomy(settings)

    async def main() -> Outcome:
        async with _services(settings) as svc:
            with svc.engine.begin() as conn:
                ids = _typed_ids(
                    conn, event_type, tax.version, _parse_day(since) if since else None
                )
            return await ingest.fetch_attachments_for(svc, ids, job="extract_fetch", limit=limit)

    outcome = asyncio.run(main())
    typer.echo(
        f"{event_type}: {outcome.n_records} attempted, {outcome.n_new} downloaded {outcome.meta}"
    )


@extract_app.command("texts")
def extract_texts(
    limit: Annotated[int, typer.Option(help="Most documents in this run.")] = 10**7,
) -> None:
    """Extract text from every downloaded attachment not yet extracted."""
    from gats.extract.texts import extract_pending

    settings = _settings()
    engine = make_engine(settings.resolved_db_url)
    init_db(engine)
    with engine.begin() as conn:
        stats = extract_pending(conn, RawStore(settings.raw_dir), utcnow(), limit=limit)
    engine.dispose()
    typer.echo(
        f"{stats.documents} documents: {stats.with_text} with text, "
        f"{stats.needs_ocr} need OCR, {stats.errors} errors"
    )
    for sample in stats.error_samples:
        typer.echo(f"  error: {sample}")


@extract_app.command("coverage")
def extract_coverage(
    event_type: Annotated[str, typer.Option("--type", help="Event type, e.g. ORDER_WIN.")],
) -> None:
    """Share of an event type's downloaded attachments that yield text (target >= 90%)."""
    from gats.extract.texts import text_coverage

    settings = _settings(log_to_file=False)
    tax = _taxonomy(settings)
    engine = make_engine(settings.resolved_db_url)
    init_db(engine)
    with engine.begin() as conn:
        cov = text_coverage(conn, event_type, tax.version)
    engine.dispose()
    typer.echo(
        f"{event_type}: {cov.filings} filings with attachments, {cov.downloaded} downloaded; "
        f"text {cov.with_text} ({cov.share:.1%}), needs OCR {cov.needs_ocr}, errors {cov.errors}"
    )
    typer.echo(f"  attachment status: {cov.by_status}")


@extract_app.command("run")
def extract_run(
    event_type: Annotated[
        str, typer.Option("--type", help="Event type (ORDER_WIN).")
    ] = "ORDER_WIN",
    mode: Annotated[str, typer.Option(help="rules | llm | cascade")] = "cascade",
    limit: Annotated[int, typer.Option(help="Most filings in this run.")] = 50,
) -> None:
    """Extract facts (amount, counterparty, ...) from filings' attachment text."""
    from gats.extract.cascade import run_extractions

    if mode not in ("rules", "llm", "cascade"):
        raise typer.BadParameter("mode must be rules, llm or cascade")
    settings = _settings()
    tax = _taxonomy(settings)

    async def main() -> Any:
        async with _services(settings) as svc:
            return await run_extractions(
                svc,
                event_type=event_type,
                taxonomy_version=tax.version,
                mode=mode,  # type: ignore[arg-type]
                limit=limit,
            )

    stats = asyncio.run(main())
    typer.echo(f"{stats.version}: {stats.filings} filings {dict(stats.by_method)}")
    if stats.llm_calls or stats.llm_cached:
        mean = stats.llm_ms / stats.llm_calls / 1000 if stats.llm_calls else 0.0
        typer.echo(
            f"  LLM: {stats.llm_calls} calls (mean {mean:.1f} s), {stats.llm_cached} cached, "
            f"status {dict(stats.llm_status)}"
        )
    if stats.deferred:
        typer.echo(f"  {stats.deferred} filings left pending (no LLM answer)")
    if stats.stopped:
        typer.echo(f"  stopped early: {stats.stopped}", err=True)
        raise typer.Exit(1)


label_app = typer.Typer(no_args_is_help=True, help="Human labels for extraction (T4.5).")
app.add_typer(label_app, name="label")

_SAMPLE = Path("labels/order_win_v1.sample.json")
_LABELS = Path("labels/order_win_v1.jsonl")
SamplePath = Annotated[Path, typer.Option("--sample", help="Sample file.")]
LabelsPath = Annotated[Path, typer.Option("--labels", help="Labels file (JSONL).")]


class _TyperUI:
    """The labelling prompts, in the terminal."""

    def show(self, text: str = "", *, highlight: bool = False) -> None:
        typer.echo(typer.style(text, bold=True) if highlight else text)

    def ask(self, prompt: str, *, default: str | None = None) -> str:
        return str(typer.prompt(prompt, default=default, show_default=default is not None))

    def page(self, text: str) -> None:
        import textwrap

        typer.echo("-" * 30 + " full text " + "-" * 30)
        typer.echo(textwrap.fill(" ".join(text.split()), width=100))
        typer.echo("-" * 71)


def _load_sample(path: Path) -> Any:
    from gats.extract.labels import Sample

    if not path.exists():
        typer.echo(f"no sample at {path} (run from the repo root; draw one with gats label sample)")
        raise typer.Exit(1)
    return Sample.load(path)


@label_app.command("sample")
def label_sample(
    start: Annotated[str, typer.Option(help="First filing day, YYYY-MM-DD.")],
    end: Annotated[str, typer.Option(help="Last filing day, YYYY-MM-DD.")],
    n: Annotated[int, typer.Option(help="Items to draw: the labels needed plus spares.")] = 330,
    seed: Annotated[int, typer.Option(help="Random seed (recorded in the file).")] = 20261003,
    source: Annotated[str, typer.Option(help="BSE, NSE or ALL.")] = "NSE",
    out: SamplePath = _SAMPLE,
    force: Annotated[bool, typer.Option(help="Replace an existing sample.")] = False,
) -> None:
    """Draw the evaluation sample, once, before anyone labels (commit the file)."""
    from gats.extract.labels import draw_sample

    if out.exists() and not force:
        typer.echo(
            f"{out} exists. Labels refer to it; redraw (--force) only before labelling starts."
        )
        raise typer.Exit(1)
    settings = _settings()
    tax = _taxonomy(settings)
    engine = make_engine(settings.resolved_db_url)
    init_db(engine)
    with engine.begin() as conn:
        sample = draw_sample(
            conn,
            name=out.name.removesuffix(".sample.json"),
            event_type="ORDER_WIN",
            taxonomy_version=tax.version,
            start=_parse_day(start),
            end=_parse_day(end),
            source=None if source.upper() == "ALL" else source.upper(),
            n=n,
            seed=seed,
            now=utcnow(),
        )
    sample.save(out)
    typer.echo(f"{len(sample.items)} of {sample.population} filings -> {out}")
    for stratum, counts in sample.strata.items():
        typer.echo(f"  {stratum}: {counts['sample']} of {counts['population']}")


@label_app.command("prepare")
def label_prepare(
    sample_path: SamplePath = _SAMPLE,
    limit: Annotated[int, typer.Option(help="Most filings in this run.")] = 10_000,
) -> None:
    """Ask the LLM about every sampled filing before review (cached and
    resumable; about 18 s a filing on a laptop GPU)."""
    from gats.extract.cascade import run_extractions
    from gats.extract.labels import resolve

    sample = _load_sample(sample_path)
    settings = _settings()

    async def main() -> Any:
        async with _services(settings) as svc:
            with svc.engine.begin() as conn:
                ids = resolve(conn, sample)
            return await run_extractions(
                svc,
                event_type=sample.event_type,
                taxonomy_version=sample.taxonomy_version,
                mode="llm",
                limit=limit,
                ids=set(ids.values()),
            )

    stats = asyncio.run(main())
    typer.echo(
        f"{stats.filings} filings: {stats.llm_calls} LLM calls, {stats.llm_cached} cached, "
        f"status {dict(stats.llm_status)}"
    )
    if stats.stopped:
        typer.echo(f"stopped early: {stats.stopped}", err=True)
        raise typer.Exit(1)


@label_app.command("review")
def label_review(
    sample_path: SamplePath = _SAMPLE,
    labels_path: LabelsPath = _LABELS,
    redo_skipped: Annotated[bool, typer.Option(help="Show skipped items again.")] = False,
) -> None:
    """Check each proposal against the filing and record the truth.

    Resumable: every decision is saved at once, and 'quit' stops cleanly.
    """
    from gats.extract.labels import (
        append_label,
        load_labels,
        resolve,
        review_entry,
        review_one,
    )

    sample = _load_sample(sample_path)
    settings = _settings()
    ui = _TyperUI()

    async def main() -> None:
        done = load_labels(labels_path)
        todo = [
            item
            for item in sample.items
            if item.doc_id not in done
            or (redo_skipped and done[item.doc_id]["status"] == "skipped")
        ]
        typer.echo(
            f"{len(sample.items) - len(todo)} done, {len(todo)} to go. See labels/README.md."
        )
        llm_ok = True
        async with _services(settings) as svc:
            with svc.engine.begin() as conn:
                ids = resolve(conn, sample)
            for item in todo:
                entry = await review_entry(svc, item, ids.get(item.doc_id), llm_ok=llm_ok)
                if entry is None:
                    if llm_ok:
                        typer.echo(
                            "The LLM gave no answer; its items stay pending (start Ollama, "
                            "or run gats label prepare). Continuing with the rules' items."
                        )
                    llm_ok = False
                    continue
                labelled = len(load_labels(labels_path).keys() & {i.doc_id for i in sample.items})
                record = review_one(ui, entry, f"{labelled + 1}/{len(sample.items)}", utcnow)
                if record is None:
                    break
                append_label(labels_path, record)

    asyncio.run(main())
    _print_label_stats(sample, labels_path)


def _print_label_stats(sample: Any, labels_path: Path) -> None:
    from gats.extract.labels import load_labels, summarize

    for line in summarize(sample, load_labels(labels_path)):
        typer.echo(line)


@label_app.command("stats")
def label_stats(sample_path: SamplePath = _SAMPLE, labels_path: LabelsPath = _LABELS) -> None:
    """Labelling progress, decisions, and how often each proposer was accepted."""
    _print_label_stats(_load_sample(sample_path), labels_path)


def _last_weekday_before(day: date) -> date:
    day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


async def _probe_upstox(
    settings: Settings, target: ProbeTarget, when: date | None, symbol: str, probes_dir: Path
) -> int:
    """The Upstox probes: the instrument file (public) and one day of
    one-minute candles (needs the Analytics Token), checked against our own
    NSE end-of-day row."""
    from gats.db.schema import eod_prices
    from gats.marketdata.bars import eod_check, summarize
    from gats.marketdata.upstox import TokenMissing, auth_headers, key_for_symbol
    from gats.sources import upstox

    async with _services(settings) as svc:
        s = svc.settings
        stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
        if target is ProbeTarget.upstox_instruments:
            try:
                got = await svc.client.get(s.upstox_instruments_url)
            except FetchError as exc:
                typer.echo(f"FETCH FAILED: {exc}")
                return 1
            kind, meta, suffix = upstox.INSTRUMENTS_KIND, {}, ".json.gz"
        else:
            the_day = when or _last_weekday_before(ist_today())
            with svc.engine.begin() as conn:
                key = key_for_symbol(conn, symbol)
            if key is None:
                typer.echo(f"no ISIN for {symbol} yet: run `gats probe instruments` first")
                return 1
            try:
                headers = auth_headers(svc)
            except TokenMissing as exc:
                typer.echo(str(exc))
                return 1
            url = upstox.candles_url(s.upstox_api_base, key, "minutes", 1, the_day, the_day)
            try:
                got = await svc.client.get(url, headers=headers)
            except FetchError as exc:
                typer.echo(f"FETCH FAILED: {exc}")
                return 1
            kind, suffix = upstox.CANDLES_KIND, ".json"
            meta = {"instrument_key": key, "from": the_day.isoformat(), "to": the_day.isoformat()}
        sample_path = probes_dir / f"{target.value}-{stamp}{suffix}"
        sample_path.write_bytes(got.content)
        with svc.engine.begin() as conn:
            repo.save_raw(
                conn,
                svc.store,
                got.content,
                kind=kind,
                source=upstox.SOURCE,
                url=got.url,
                content_type=got.content_type,
                fetched_at=got.fetched_at,
                meta=meta,
            )
        typer.echo(f"url:          {got.url}")
        typer.echo(f"http status:  {got.status}")
        typer.echo(f"size:         {len(got.content):,} bytes  ({got.elapsed_ms} ms)")
        typer.echo(f"saved to:     {sample_path}")
        if not got.ok:
            typer.echo(f"first bytes:  {got.content[:300]!r}")
            return 1
        try:
            if target is ProbeTarget.upstox_instruments:
                instruments = upstox.parse_instruments(got.content)
                types = Counter((r.segment, r.instrument_type) for r in instruments.records)
                typer.echo(f"meta:         {instruments.meta}")
                typer.echo(f"kept by type: {dict(types.most_common(12))}")
                for warning in instruments.warnings[:10]:
                    typer.echo(f"warning: {warning}")
                return 0
            bars = upstox.parse_candles(got.content, instrument_key=meta["instrument_key"])
        except PayloadError as exc:
            typer.echo(f"PARSE FAILED: {exc}")
            return 1
        for warning in bars.warnings[:10]:
            typer.echo(f"warning: {warning}")
        summary = summarize(bars.records)
        if summary is None:
            typer.echo(f"no bars for {symbol} on {the_day} (a holiday?)")
            return 1
        typer.echo(
            f"bars:         {summary.bars} from {to_ist(summary.first):%H:%M} to "
            f"{to_ist(summary.last):%H:%M} IST"
        )
        with svc.engine.begin() as conn:
            eod = conn.execute(
                select(eod_prices).where(
                    eod_prices.c.trade_date == the_day,
                    eod_prices.c.symbol == symbol.upper(),
                    eod_prices.c.series == "EQ",
                )
            ).first()
        if eod is None:
            typer.echo(f"no NSE EOD row for {symbol} on {the_day} to compare with")
            return 0
        failures = 0
        for name, ours, theirs, agrees in eod_check(
            summary, open=eod.open, high=eod.high, low=eod.low, volume=eod.volume
        ):
            failures += not agrees
            typer.echo(f"  {name:7} bars {ours}  NSE EOD {theirs}  {'ok' if agrees else 'DIFFERS'}")
        return 1 if failures else 0


bars_app = typer.Typer(no_args_is_help=True, help="One-minute bars (M5).")
app.add_typer(bars_app, name="bars")


def _bar_key(conn: Any, symbol: str | None, key: str | None) -> str:
    from gats.marketdata.upstox import key_for_symbol

    if key:
        return key
    if not symbol:
        raise typer.BadParameter("give --symbol or --key")
    found = key_for_symbol(conn, symbol)
    if found is None:
        typer.echo(f"no ISIN for {symbol} yet: run `gats probe instruments` first")
        raise typer.Exit(1)
    return found


@bars_app.command("fetch")
def bars_fetch(
    start: Annotated[str, typer.Option(help="First day, YYYY-MM-DD (its whole month).")],
    end: Annotated[str, typer.Option(help="Last day, YYYY-MM-DD (its whole month).")],
    symbol: Annotated[str | None, typer.Option(help="NSE symbol.")] = None,
    key: Annotated[str | None, typer.Option(help="Upstox instrument key instead.")] = None,
    force: Annotated[bool, typer.Option(help="Refetch complete months.")] = False,
) -> None:
    """Fetch one-minute bars month by month (resumable; needs the Analytics Token)."""
    from gats.marketdata.bars import months
    from gats.marketdata.upstox import TokenMissing, fetch_month

    settings = _settings()

    async def main() -> int:
        async with _services(settings) as svc:
            with svc.engine.begin() as conn:
                instrument = _bar_key(conn, symbol, key)
            failed = 0
            for month in months(_parse_day(start), _parse_day(end)):
                try:
                    result = await fetch_month(svc, instrument, month, force=force)
                except TokenMissing as exc:
                    typer.echo(str(exc))
                    return 1
                failed += result.status == "failed"
                note = f" ({result.error})" if result.error else ""
                typer.echo(f"{instrument} {month:%Y-%m}: {result.status}, {result.bars} bars{note}")
            return 1 if failed else 0

    raise typer.Exit(asyncio.run(main()))


@bars_app.command("show")
def bars_show(
    day: Annotated[str, typer.Option("--date", help="YYYY-MM-DD.")],
    symbol: Annotated[str | None, typer.Option(help="NSE symbol.")] = None,
    key: Annotated[str | None, typer.Option(help="Upstox instrument key instead.")] = None,
) -> None:
    """One day's stored bars: count, first and last minute, OHLC, volume."""
    from gats.marketdata.bars import read_bars, summarize

    settings = _settings(log_to_file=False)
    engine = make_engine(settings.resolved_db_url)
    init_db(engine)
    with engine.begin() as conn:
        instrument = _bar_key(conn, symbol, key)
    the_day = _parse_day(day)
    bars = read_bars(
        settings.bars_dir,
        instrument,
        ist_datetime(the_day, time()),
        ist_datetime(the_day + timedelta(days=1), time()),
    )
    summary = summarize(bars)
    if summary is None:
        typer.echo(f"no bars stored for {instrument} on {the_day}")
        raise typer.Exit(1)
    typer.echo(
        f"{instrument} {the_day}: {summary.bars} bars {to_ist(summary.first):%H:%M}-"
        f"{to_ist(summary.last):%H:%M} IST  O {summary.open} H {summary.high} "
        f"L {summary.low} C {summary.close}  V {summary.volume:,}"
    )


EventTypes = Annotated[
    list[str] | None,
    typer.Option("--type", help="Event type; repeat for more (default ORDER_WIN)."),
]


def _windows(settings: Settings, types: list[str] | None, start: str, end: str | None) -> Any:
    from gats.marketdata.upstox import current_isins
    from gats.marketdata.windows import event_windows

    tax = _taxonomy(settings)
    engine = make_engine(settings.resolved_db_url)
    init_db(engine)
    with engine.begin() as conn:
        return event_windows(
            AsOf(conn, utcnow()),
            event_types=set(types or ["ORDER_WIN"]),
            taxonomy_version=tax.version,
            start=_parse_day(start),
            end=_parse_day(end) if end else ist_today(),
            current_isins=current_isins(conn),
        )


def _print_window_coverage(settings: Settings, windows: Any, index_key: str) -> None:
    from gats.marketdata.windows import bars_per_day, window_coverage

    cov = window_coverage(windows, bars_per_day(settings.bars_dir), index_key)
    typer.echo(f"coverage: {cov.covered}/{cov.events} events ({cov.share:.1%}; target 95%)")
    for year, (covered, events) in sorted(cov.by_year.items()):
        typer.echo(f"  {year}: {covered}/{events}")
    for window in cov.missing:
        typer.echo(
            f"  missing: #{window.announcement_id} {window.event_type} {window.instrument_key} "
            f"sessions {', '.join(d.isoformat() for d in window.sessions)}"
        )


@bars_app.command("events")
def bars_events(
    types: EventTypes = None,
    start: Annotated[str, typer.Option(help="First filing day.")] = "2022-01-01",
    end: Annotated[str | None, typer.Option(help="Last filing day (default today).")] = None,
    index: Annotated[str, typer.Option(help="Benchmark index name.")] = "Nifty 500",
    limit: Annotated[int | None, typer.Option(help="Most requests in this run.")] = None,
) -> None:
    """Fetch bars around in-scope events: the previous, event and next
    sessions, for the stock and the index (resumable; needs the token)."""
    from gats.marketdata.upstox import TokenMissing
    from gats.marketdata.windows import fetch_needed, months_needed

    settings = _settings()
    windows, dropped = _windows(settings, types, start, end)
    index_key = f"NSE_INDEX|{index}"
    need = months_needed(windows, index_key)
    typer.echo(
        f"{len(windows)} events (dropped: {dict(dropped)}); "
        f"{sum(len(m) for m in need.values())} instrument-months touched"
    )

    async def main() -> Any:
        async with _services(settings) as svc:
            return await fetch_needed(svc, need, limit=limit)

    try:
        statuses = asyncio.run(main())
    except TokenMissing as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    typer.echo(f"months: {dict(statuses)}")
    _print_window_coverage(settings, windows, index_key)


@bars_app.command("coverage")
def bars_coverage(
    types: EventTypes = None,
    start: Annotated[str, typer.Option(help="First filing day.")] = "2022-01-01",
    end: Annotated[str | None, typer.Option(help="Last filing day (default today).")] = None,
    index: Annotated[str, typer.Option(help="Benchmark index name.")] = "Nifty 500",
) -> None:
    """Share of in-scope events whose whole window has bars (T5.2 target: 95%)."""
    settings = _settings(log_to_file=False)
    windows, dropped = _windows(settings, types, start, end)
    typer.echo(f"{len(windows)} events (dropped: {dict(dropped)})")
    _print_window_coverage(settings, windows, f"NSE_INDEX|{index}")


# Orders sent to the broker's calculator to check the cost model (T6.1).
_CHARGE_EXAMPLES: tuple[tuple[str, str, int, float], ...] = (
    ("intraday", "buy", 100, 1000.0),
    ("intraday", "sell", 100, 1010.0),
    ("intraday", "buy", 10, 100.0),  # brokerage capped at 0.1%
    ("delivery", "buy", 50, 2000.0),
    ("delivery", "sell", 50, 2000.0),  # pays DP charges
)


async def _probe_upstox_charges(settings: Settings, symbol: str, probes_dir: Path) -> int:
    """The broker's own calculator vs our cost model, order by order."""
    from gats.backtest.costs import CostModel, Fill
    from gats.marketdata.upstox import TokenMissing, auth_headers, key_for_symbol
    from gats.sources import upstox

    model = CostModel.load(Path("configs/costs/india_equity.yaml"))
    differences = 0
    async with _services(settings) as svc:
        with svc.engine.begin() as conn:
            key = key_for_symbol(conn, symbol)
        if key is None:
            typer.echo(f"no ISIN for {symbol} yet: run `gats probe instruments` first")
            return 1
        try:
            headers = auth_headers(svc)
        except TokenMissing as exc:
            typer.echo(str(exc))
            return 1
        for product, side, quantity, price in _CHARGE_EXAMPLES:
            url = upstox.charges_url(
                svc.settings.upstox_api_base, key, quantity,
                "D" if product == "delivery" else "I", side.upper(), price,
            )  # fmt: skip
            try:
                got = await svc.client.get(url, headers=headers)
            except FetchError as exc:
                typer.echo(f"FETCH FAILED: {exc}")
                return 1
            with svc.engine.begin() as conn:
                repo.save_raw(
                    conn, svc.store, got.content, kind=upstox.CHARGES_KIND,
                    source=upstox.SOURCE, url=got.url, content_type=got.content_type,
                    fetched_at=got.fetched_at,
                    meta={"product": product, "side": side, "quantity": quantity, "price": price},
                )  # fmt: skip
            stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
            (probes_dir / f"upstox-charges-{product}-{side}-{quantity}-{stamp}.json").write_bytes(
                got.content
            )
            typer.echo(f"{product} {side} {quantity} x {price}: HTTP {got.status}")
            if not got.ok:
                typer.echo(f"  first bytes: {got.content[:300]!r}")
                return 1
            try:
                theirs = upstox.parse_charges(got.content)
            except PayloadError as exc:
                typer.echo(f"  PARSE FAILED: {exc}")
                return 1
            ours = model.charges(Fill(side, product, price, quantity, ist_today()))  # type: ignore[arg-type]
            mine = {name: getattr(ours, name) for name in theirs if hasattr(ours, name)}
            mine["total"] = ours.total
            for name, value in theirs.items():
                expected = mine.get(name)
                same = expected is not None and abs(expected - value) <= 0.01
                differences += not same
                shown = "-" if expected is None else f"{expected:.2f}"
                verdict = "ok" if same else "DIFFERS"
                typer.echo(f"  {name:21} broker {value:10.2f}  model {shown:>10}  {verdict}")
    return 1 if differences else 0


experiments_app = typer.Typer(no_args_is_help=True, help="The experiment registry (T6.6).")
app.add_typer(experiments_app, name="experiments")


@experiments_app.command("list")
def experiments_list(
    kind: Annotated[str | None, typer.Option(help="event_study or backtest.")] = None,
    limit: Annotated[int, typer.Option(help="Most recent runs to show.")] = 20,
) -> None:
    """Recent research runs and the number of distinct designs tried."""
    from gats.research.registry import recent, trial_count

    settings = _settings(log_to_file=False)
    engine = make_engine(settings.resolved_db_url)
    init_db(engine)
    with engine.begin() as conn:
        rows = recent(conn, kind=kind, limit=limit)
        designs = trial_count(conn, kind=kind)
    engine.dispose()
    typer.echo(f"{designs} distinct design(s) tried" + (f" of kind {kind}" if kind else ""))
    for row in rows:
        sha = (row.git_sha or "no-git")[:7] + ("*" if row.git_dirty else "")
        window = f"{row.data_start}..{row.data_end}"
        held = " HOLDOUT" if row.holdout else ""
        typer.echo(
            f"#{row.id} {to_ist(row.started_at):%Y-%m-%d %H:%M} {row.kind} {row.name} "
            f"{row.params_hash[:12]} {window}{held} {sha} {row.status}"
        )
        metrics = row.metrics or {}
        shown = {k: metrics[k] for k in ("events", "trades", "net_pnl", "sharpe") if k in metrics}
        if shown or row.error:
            typer.echo(f"     {shown if shown else ''}{' ' + row.error if row.error else ''}")


backtest_app = typer.Typer(no_args_is_help=True, help="Backtests (M6).")
app.add_typer(backtest_app, name="backtest")


def _load_strategy(path: Path) -> Any:
    from gats.strategy.catalog import load_strategy

    try:
        return load_strategy(path)
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc


@backtest_app.command("run")
def backtest_run(
    start: Annotated[str, typer.Option(help="First filing day, YYYY-MM-DD.")],
    end: Annotated[str, typer.Option(help="Last filing day, YYYY-MM-DD.")],
    strategy_path: Annotated[Path, typer.Option("--strategy", help="Strategy YAML.")] = Path(
        "configs/strategies/order_win_drift.yaml"
    ),
    types: EventTypes = None,
    cash: Annotated[float, typer.Option(help="Starting cash, rupees.")] = 1_000_000.0,
    # The risk engine sizes an order at its worst-case (limit) price, so the
    # default stays well inside the 10%-of-equity position cap.
    notional: Annotated[float, typer.Option(help="Rupees per trade.")] = 50_000.0,
    latency_s: Annotated[float, typer.Option(help="Decision to market, seconds.")] = 5.0,
    holdout: Annotated[bool, typer.Option(help="This run reads the test period.")] = False,
    prereg: Annotated[
        Path | None, typer.Option(help="Pre-registration recording the design hash.")
    ] = None,
    report: Annotated[Path, typer.Option(help="Where to write the report.")] = Path(
        "reports/M6_backtest.md"
    ),
) -> None:
    """Run a registered backtest over stored events and bars and write the G2 report."""
    from gats.backtest import feed
    from gats.backtest.costs import CostModel
    from gats.backtest.engine import EngineConfig
    from gats.backtest.ledger import daily_returns
    from gats.backtest.report import g2_checks, g2_verdict, render
    from gats.backtest.runner import HoldoutError, design, run_backtest
    from gats.backtest.validation import deflated_sharpe, registry_trials
    from gats.risk.engine import RiskEngine

    settings = _settings()
    strategy = _load_strategy(strategy_path)
    costs = CostModel.load(Path("configs/costs/india_equity.yaml"))
    config = EngineConfig(initial_cash=cash, notional_per_trade=notional, latency_s=latency_s)
    windows, dropped = _windows(settings, types, start, end)
    first, last = _parse_day(start), _parse_day(end)
    db = make_engine(settings.resolved_db_url)
    init_db(db)
    with db.begin() as conn:
        clock = AsOf(conn, utcnow())
        lookups = feed.Lookups(clock, windows)
        base = RiskEngine.load(Path("configs/risk.yaml"))
        # Surveillance history starts when recording did; a backtest of
        # earlier days cannot know it, and says so in its design.
        limits = base.limits.model_copy(update={"unknown_flags": "allow"})
        risk = RiskEngine(
            limits,
            f"{base.version}+unknown-flags-allow",
            liquidity=lookups.liquidity,
            flags=lookups.flags,
        )
        extracted = clock.extracted_facts([w.announcement_id for w in windows], "cascade:")
        facts = feed.with_revenue_ratio(clock, windows, extracted)
        bars = feed.bar_events(settings.bars_dir, windows)
        sessions = clock.calendar().trading_days(first, last, include_special=False)
        typer.echo(
            f"{len(windows)} events (dropped: {dict(dropped)}), {len(facts)} with extracted "
            f"facts, {len(bars):,} bars; design {design(strategy, costs, config, risk.version)[0]}"
        )
        if not bars:
            typer.echo("no bars stored for these events: run `gats bars events` first")
            raise typer.Exit(1)
        try:
            result, metrics, run_id = run_backtest(
                db,
                strategy,
                [*feed.market_events(windows, facts), *bars],
                costs=costs,
                config=config,
                data_start=first,
                data_end=last,
                holdout=holdout,
                risk=risk,
                risk_version=risk.version,
                sessions=sessions,
                prereg=prereg,
            )
        except HoldoutError as exc:
            typer.echo(f"REFUSED: {exc}")
            raise typer.Exit(1) from exc
    with db.begin() as conn:
        trials, variance = registry_trials(conn)
    db.dispose()
    returns = daily_returns(result.equity, result.initial_cash, sessions)
    deflated = (
        deflated_sharpe(returns, trial_variance=variance or 0.0, trials=trials)
        if trials <= 1 or variance is not None
        else None  # several designs but no spread of Sharpe ratios to deflate with
    )
    checks = g2_checks(
        metrics,
        holdout=holdout,
        deflated_sharpe=deflated,
        trials=trials,
        max_drawdown_limit=limits.max_drawdown_pct_equity,
    )
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        render(
            run_id=run_id,
            design=design(strategy, costs, config, risk.version)[1],
            window=f"{first} to {last}",
            metrics=metrics,
            checks=checks,
        ),
        encoding="utf-8",
        newline="\n",
    )
    typer.echo(
        f"experiment #{run_id}: {metrics.trades} trades, net Rs {metrics.net_pnl:,.2f}; "
        f"G2: {g2_verdict(checks)} -> {report}"
    )


@extract_app.command("evaluate")
def extract_evaluate(
    sample_path: SamplePath = _SAMPLE,
    labels_path: LabelsPath = _LABELS,
    report: Annotated[Path, typer.Option(help="Where to write the report.")] = Path(
        "reports/M4_extraction.md"
    ),
    llm: Annotated[bool, typer.Option(help="Also score the LLM and the cascade.")] = True,
) -> None:
    """Score the rules, the LLM and the cascade against the human labels (T4.6)."""
    from gats.extract.cascade import code_hash, extractor_version
    from gats.extract.evaluate import LlmUnavailable, evaluate, predict, render
    from gats.extract.labels import TARGET_LABELS, load_labels
    from gats.extract.llm import prompt_hash

    sample = _load_sample(sample_path)
    in_sample = {item.doc_id for item in sample.items}
    records = [r for doc, r in load_labels(labels_path).items() if doc in in_sample]
    labelled = [r for r in records if r.get("status") == "labelled"]
    if not labelled:
        typer.echo(f"no labels in {labels_path} yet: run `gats label review`")
        raise typer.Exit(1)
    settings = _settings()

    async def main() -> Any:
        async with _services(settings) as svc:
            return await predict(svc, labelled, use_llm=llm)

    try:
        predictions = asyncio.run(main())
    except LlmUnavailable as exc:
        typer.echo(f"{exc}. Start Ollama (or run `gats label prepare`), or pass --no-llm.")
        raise typer.Exit(1) from exc
    result = evaluate(labelled, predictions)
    prompt = prompt_hash(settings.llm_max_chars)
    versions = {
        "rules": extractor_version("rules", prompt, settings.llm_model),
        "llm": extractor_version("llm", prompt, settings.llm_model),
        "code": code_hash(),
    }
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        render(result, target_labels=TARGET_LABELS, versions=versions),
        encoding="utf-8",
        newline="\n",
    )
    for method, score in result.scores.items():
        accuracy = score.accuracy("amount")
        shown = "n/a" if accuracy is None else f"{accuracy:.1%}"
        typer.echo(f"{method:8} amount within 1%: {shown} of {score.n} new orders")
    typer.echo(f"{result.labelled} labels -> {report}")


@research_app.command("reaction")
def research_reaction(
    scope: Annotated[
        list[str],
        typer.Option("--scope", help="Type in scope after G1; repeat for more. Logged."),
    ],
    config: Annotated[Path, typer.Option(help="Study config (YAML).")] = Path(
        "configs/studies/m5_reaction.yaml"
    ),
    prereg: Annotated[
        Path, typer.Option(help="Pre-registration recording the config's hash.")
    ] = Path("docs/research/M5_prereg.md"),
    report: Annotated[Path, typer.Option(help="Where to write the report.")] = Path(
        "reports/M5_reaction_curves.md"
    ),
) -> None:
    """Run the pre-registered M5 intraday reaction study (gate G1b).

    Refuses unless the config is the registered one, bars cover the events,
    and feed latency can be measured from live recording.
    """
    from sqlalchemy import func

    from gats.backtest.costs import CostModel
    from gats.db.schema import experiments
    from gats.marketdata.upstox import current_isins
    from gats.marketdata.windows import bars_per_day, event_windows, window_coverage
    from gats.research.event_study import write_parquet
    from gats.research.reaction import (
        NotReady,
        measured_delay,
        run_reaction_study,
        verify_reaction,
    )
    from gats.research.reaction_report import build_reaction_report
    from gats.research.registry import experiment
    from gats.research.runs import stamp
    from gats.research.study import RegistrationError

    settings = _settings()
    try:
        cfg, digest = verify_reaction(config, prereg)
    except RegistrationError as exc:
        typer.echo(f"REFUSED: {exc}")
        raise typer.Exit(1) from exc
    outside = sorted(set(scope) - set(cfg.events.confirmatory))
    if outside:
        typer.echo(f"REFUSED: {outside} are not confirmatory types of this study")
        raise typer.Exit(1)
    index_key = f"NSE_INDEX|{cfg.benchmark.index}"
    costs = CostModel.load(cfg.costs.cost_file)
    db = make_engine(settings.resolved_db_url)
    init_db(db)
    now = utcnow()
    with db.begin() as conn:
        clock = AsOf(conn, now)
        windows, _ = event_windows(
            clock,
            event_types=set(scope),
            taxonomy_version=cfg.taxonomy_version,
            start=cfg.data.start,
            end=cfg.data.end,
            source=cfg.data.source,
            current_isins=current_isins(conn),
            exclude_categories=cfg.events.exclude_categories,
        )
        coverage = window_coverage(windows, bars_per_day(settings.bars_dir), index_key)
        try:
            if coverage.share < 0.95:
                raise NotReady(
                    f"bars cover {coverage.covered}/{coverage.events} in-scope events "
                    f"({coverage.share:.1%}; the study needs 95%): run `gats bars events`"
                )
            delay = measured_delay(conn, cfg, now)
            median_delay = measured_delay(conn, cfg, now, percentile=50)
        except NotReady as exc:
            typer.echo(f"NOT READY: {exc}")
            raise typer.Exit(1) from exc
        m3_run = conn.execute(
            select(func.min(experiments.c.started_at)).where(
                experiments.c.kind == "event_study", experiments.c.status == "done"
            )
        ).scalar()
        registered = experiment(
            db,
            kind="reaction_study",
            name=cfg.study,
            params_hash=digest,
            params={"scope": sorted(scope), "delay_s": delay.total_s, "feed_s": delay.feed_s},
            data_start=cfg.data.start,
            data_end=cfg.data.end,
            holdout=True,  # the study reads its test period
        )
        with registered as run:
            rows = run_reaction_study(
                clock, cfg, settings.bars_dir, delay_s=delay.total_s, scope=scope,
                costs=costs, index_key=index_key,
            )  # fmt: skip
            median_rows = run_reaction_study(
                clock, cfg, settings.bars_dir, delay_s=median_delay.total_s, scope=scope,
                costs=costs, index_key=index_key,
            )  # fmt: skip
            run.metrics = {"events": len(rows)}
    db.dispose()
    run_id = stamp()
    out = settings.data_dir / "research" / cfg.study / run_id
    write_parquet(rows, out / "events.parquet")
    text, family = build_reaction_report(
        rows,
        cfg,
        digest=digest,
        run_id=run_id,
        experiment_id=run.id,
        scope=scope,
        delay=delay,
        median_rows=median_rows,
        median_delay=median_delay,
        m3_run_date=to_ist(m3_run).date() if m3_run else None,
    )
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(text, encoding="utf-8", newline="\n")
    passed = [f"{c.event_type}/{c.exit}" for c in family if c.passes]
    typer.echo(f"run {run_id}: {len(rows)} events -> {out / 'events.parquet'}")
    typer.echo(f"G1b: {'PASS ' + ', '.join(passed) if passed else 'nothing passes'} -> {report}")


@research_app.command("magnitude")
def research_magnitude(
    config: Annotated[Path, typer.Option(help="Study config (YAML).")] = Path(
        "configs/studies/m4_magnitude.yaml"
    ),
    prereg: Annotated[
        Path, typer.Option(help="Pre-registration recording the config's hash.")
    ] = Path("docs/research/M4_prereg.md"),
    labels_path: LabelsPath = _LABELS,
    report: Annotated[Path, typer.Option(help="Where to write the report.")] = Path(
        "reports/M4_magnitude.md"
    ),
) -> None:
    """Run the pre-registered order-magnitude study (T4.7).

    Refuses an edited config, incomplete data, or a missing extraction
    evaluation (the report must quote the measured accuracy).
    """
    from gats.extract.labels import TARGET_LABELS, load_labels
    from gats.research.event_study import run_event_study, to_records, write_parquet
    from gats.research.magnitude import build_magnitude_report, size_filter, verify_magnitude
    from gats.research.registry import experiment
    from gats.research.runs import data_readiness, stamp
    from gats.research.study import RegistrationError

    settings = _settings()
    try:
        cfg, digest = verify_magnitude(config, prereg)
    except RegistrationError as exc:
        typer.echo(f"REFUSED: {exc}")
        raise typer.Exit(1) from exc
    labelled = sum(r.get("status") == "labelled" for r in load_labels(labels_path).values())
    evaluation = Path("reports/M4_extraction.md")
    if labelled < TARGET_LABELS or not evaluation.exists():
        has_report = "a" if evaluation.exists() else "no"
        typer.echo(
            f"NOT READY: the extraction evaluation (T4.6) needs {TARGET_LABELS} labels and its "
            f"report; there are {labelled} labels and {has_report} report."
        )
        raise typer.Exit(1)
    accuracy_note = next(
        (
            line.strip("| ").replace(" | ", ", ")
            for line in evaluation.read_text(encoding="utf-8").splitlines()
            if line.startswith("| cascade |")
        ),
        "see reports/M4_extraction.md",
    )
    db = make_engine(settings.resolved_db_url)
    init_db(db)
    exits = [x.name for x in cfg.exits]
    with db.begin() as conn:
        clock = AsOf(conn, utcnow())
        readiness = data_readiness(conn, cfg, clock.calendar())
        if not readiness.ready:
            typer.echo("NOT READY: " + "; ".join(readiness.problems()))
            raise typer.Exit(1)
        registered = experiment(
            db,
            kind="event_study",
            name=cfg.study,
            params_hash=digest,
            params={"config": config.as_posix(), "threshold": cfg.magnitude.min_amount_vs_revenue},
            data_start=cfg.data.start,
            data_end=cfg.data.end,
            holdout=True,  # the second read of M3's test period
        )
        with registered as run:
            rows = to_records(run_event_study(clock, cfg, size_filter(clock, cfg)), exits)
            exploratory = {
                threshold: to_records(
                    run_event_study(clock, cfg, size_filter(clock, cfg, threshold)), exits
                )
                for threshold in cfg.magnitude.exploratory_thresholds
            }
            run.metrics = {"events": len(rows)}
    db.dispose()
    run_id = stamp()
    out = settings.data_dir / "research" / cfg.study / run_id
    write_parquet(rows, out / "events.parquet")
    text, family = build_magnitude_report(
        rows,
        exploratory,
        cfg,
        digest=digest,
        run_id=run_id,
        experiment_id=run.id,
        accuracy_note=accuracy_note,
    )
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(text, encoding="utf-8", newline="\n")
    passed = [c.exit for c in family if c.passes]
    typer.echo(f"run {run_id}: {len(rows)} events -> {out / 'events.parquet'}")
    typer.echo(f"result: {'PASS at ' + ', '.join(passed) if passed else 'no pass'} -> {report}")


async def _probe_upstox_intraday(settings: Settings, symbol: str, probes_dir: Path) -> int:
    """Today's one-minute candles as the paper runtime gets them, and the
    three things the docs leave open: is the candle still forming in the
    reply, how soon does a finished one appear, what is served out of hours."""
    from gats.marketdata.upstox import TokenMissing, auth_headers, key_for_symbol
    from gats.sources import upstox

    async with _services(settings) as svc:
        with svc.engine.begin() as conn:
            key = key_for_symbol(conn, symbol)
        if key is None:
            typer.echo(f"no ISIN for {symbol} yet: run `gats probe instruments` first")
            return 1
        try:
            headers = auth_headers(svc)
        except TokenMissing as exc:
            typer.echo(str(exc))
            return 1
        asked_at = utcnow()
        try:
            got = await svc.client.get(
                upstox.intraday_url(svc.settings.upstox_api_base, key), headers=headers
            )
        except FetchError as exc:
            typer.echo(f"FETCH FAILED: {exc}")
            return 1
        stamp = asked_at.strftime("%Y%m%dT%H%M%SZ")
        sample_path = probes_dir / f"upstox-intraday-{stamp}.json"
        sample_path.write_bytes(got.content)
        with svc.engine.begin() as conn:
            repo.save_raw(
                conn, svc.store, got.content, kind=upstox.INTRADAY_KIND, source=upstox.SOURCE,
                url=got.url, content_type=got.content_type, fetched_at=got.fetched_at,
                meta={"instrument_key": key, "http_status": got.status},
            )  # fmt: skip
    typer.echo(f"url:          {got.url}")
    typer.echo(f"http status:  {got.status}")
    typer.echo(f"size:         {len(got.content):,} bytes  ({got.elapsed_ms} ms)")
    typer.echo(f"saved to:     {sample_path}")
    typer.echo(f"asked at:     {to_ist(asked_at):%Y-%m-%d %H:%M:%S} IST")
    if not got.ok:
        typer.echo(f"first bytes:  {got.content[:300]!r}")
        return 1
    try:
        bars = upstox.parse_candles(got.content, instrument_key=key).records
    except PayloadError as exc:
        typer.echo(f"PARSE FAILED: {exc}")
        return 1
    if not bars:
        typer.echo("candles:      none (before the open, or a day without a session?)")
        return 0
    first, last = bars[0], bars[-1]
    days = sorted({to_ist(bar.ts).date() for bar in bars})
    typer.echo(
        f"candles:      {len(bars)} from {to_ist(first.ts):%Y-%m-%d %H:%M} to "
        f"{to_ist(last.ts):%Y-%m-%d %H:%M} IST ({len(days)} day(s): "
        f"{', '.join(d.isoformat() for d in days)})"
    )
    closes_at = last.ts + timedelta(minutes=1)
    if closes_at > asked_at:
        left = (closes_at - asked_at).total_seconds()
        typer.echo(f"last candle:  STILL FORMING ({left:.0f} s of its minute left when asked)")
        if len(bars) > 1:
            age = (asked_at - (bars[-2].ts + timedelta(minutes=1))).total_seconds()
            typer.echo(f"newest finished candle closed {age:.0f} s before the question")
    else:
        age = (asked_at - closes_at).total_seconds()
        typer.echo(f"last candle:  finished; it closed {age:.0f} s before the question")
    typer.echo(
        "Run this a few times in a session (just after a minute turns), once before 09:15 and "
        "once after 15:30: the answers set GATS_PAPER_BAR_MARGIN_S and go in DATA_SOURCES.md."
    )
    return 0


paper_app = typer.Typer(
    no_args_is_help=True,
    help="Paper trading (M7): simulated fills on live data. No order is ever sent to a broker.",
)
app.add_typer(paper_app, name="paper")


@paper_app.command("run")
def paper_run(
    name: Annotated[str, typer.Option(help="The run's name. A changed design needs a new name.")],
    config: Annotated[
        Path | None, typer.Option(help="Paper config (default: configs/paper.yaml).")
    ] = None,
) -> None:
    """Run the paper runtime until Ctrl+C. The recorder must be running too:
    it is the only source of filings."""
    from gats.alerts import alerter
    from gats.marketdata.upstox import TokenMissing, auth_headers
    from gats.runtime.journal import DesignChanged, Diverged
    from gats.runtime.paper import PaperRuntime, load_system, run_paper

    settings = _settings()
    path = config or settings.paper_config_path
    try:
        system = load_system(path, settings)
    except (OSError, ValueError) as exc:  # a missing file, or one that does not validate
        typer.echo(f"cannot load the paper system from {path}: {exc}")
        raise typer.Exit(1) from exc

    async def main() -> int:
        stop = asyncio.Event()
        if sys.platform != "win32":  # Windows has no loop signal handlers; Ctrl+C still works
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, stop.set)
        async with _services(settings) as svc:
            try:
                auth_headers(svc)
                runtime = PaperRuntime(svc, system, name)
                run = runtime.run
                typer.echo(
                    f"paper run {name!r}: design {system.design_hash}, {system.strategy.version}; "
                    f"{len(run.events)} filings, {len(run.engine.orders)} orders and "
                    f"{len(run.engine.executions)} fills so far. Ctrl+C stops it."
                )
                await run_paper(svc, runtime, stop, alerter(svc))
            except (TokenMissing, DesignChanged, Diverged) as exc:
                typer.echo(f"REFUSED: {exc}")
                return 1
        return 0

    try:
        code = asyncio.run(main())
    except KeyboardInterrupt:
        typer.echo("paper run stopped")
        return
    raise typer.Exit(code)


@paper_app.command("status")
def paper_status(
    name: Annotated[
        str | None, typer.Option(help="One run in detail (default: list the runs).")
    ] = None,
) -> None:
    """What the paper runs have done, from the stored journal, orders and fills."""
    from gats.runtime.status import Latency, run_status, runs

    settings = _settings(log_to_file=False)
    db = make_engine(settings.resolved_db_url)
    init_db(db)
    with db.begin() as conn:
        listed = runs(conn)
        chosen = [r.name for r in listed if name is None or r.name == name]
        statuses = [run_status(conn, n) for n in chosen]
    db.dispose()
    if not listed:
        typer.echo("no paper runs yet: start one with `gats paper run --name <name>`")
        return
    if name is not None and not chosen:
        typer.echo(f"no paper run called {name!r} (runs: {', '.join(r.name for r in listed)})")
        raise typer.Exit(1)

    def shown(lat: Latency | None) -> str:
        if lat is None:
            return "not measured yet"
        return (
            f"median {lat.median_s:.0f} s, p95 {lat.p95_s:.0f} s, worst {lat.worst_s:.0f} s "
            f"(n={lat.n})"
        )

    for status in statuses:
        assert status is not None
        steps, orders = status.steps, status.orders
        last = (
            f"{to_ist(status.last_step_at):%Y-%m-%d %H:%M:%S} IST" if status.last_step_at else "-"
        )
        typer.echo(
            f"{status.name}: design {status.design_hash}, {status.strategy}, since "
            f"{to_ist(status.created_at):%Y-%m-%d %H:%M} IST"
        )
        typer.echo(
            f"  taken:    {steps['event']} filings, {steps['bar']:,} bars, {steps['ref']} "
            f"reference prices, {steps['close']} day closes; last {last}"
        )
        typer.echo(
            f"  orders:   {sum(orders.values())} ({orders['filled']} filled, {orders['working']} "
            f"working, {orders['expired']} expired, {orders['rejected']} rejected)"
        )
        for reason, n in status.refusals.most_common(6):
            typer.echo(f"            rejected, {reason}: {n}")
        typer.echo(
            f"  fills:    {status.fills}; bought Rs {status.bought:,.0f}, sold Rs "
            f"{status.sold:,.0f}, charges Rs {status.charges:,.2f}"
        )
        held = ", ".join(f"{key} x {n}" for key, n in sorted(status.open_quantity.items()))
        typer.echo(f"  holding:  {held or 'nothing'}")
        typer.echo(f"  feed latency (exchange to recorder):    {shown(status.feed)}")
        typer.echo(f"  hand-over latency (recorder to strategy): {shown(status.hand_over)}")
    beat = settings.paper_heartbeat_path
    if beat.exists():
        state = json.loads(beat.read_text(encoding="utf-8")).get("jobs", {}).get("paper", {})
        typer.echo(
            f"runtime: run {state.get('run')!r} last ticked {state.get('last_ok_at', 'never')}"
            + (f"; last error {state['last_error']}" if state.get("last_error") else "")
        )
    else:
        typer.echo("runtime: not running (no heartbeat file)")


# --- live pilot (M8): human-only -----------------------------------------------------

gate_app = typer.Typer(
    no_args_is_help=True,
    help="The gate in front of live trading (M8). `approve` is for a person at a terminal.",
)
app.add_typer(gate_app, name="gate")
LiveConfig = Annotated[
    Path | None, typer.Option("--config", help="Live config (default: configs/live.yaml).")
]


def _live_system(settings: Settings, config: Path | None) -> Any:
    from gats.oms.caps import load_live
    from gats.runtime.paper import load_system

    path = config or settings.live_config_path
    try:
        spec = load_live(path)
        return spec, load_system(spec.paper, settings)
    except (OSError, ValueError) as exc:
        typer.echo(f"cannot load the live system from {path}: {exc}")
        raise typer.Exit(1) from exc


@gate_app.command("status")
def gate_status(config: LiveConfig = None) -> None:
    """What stands between this system and live trading (read-only)."""
    from gats.runtime import live

    settings = _settings(log_to_file=False)
    spec, system = _live_system(settings, config)
    db = make_engine(settings.resolved_db_url)
    init_db(db)
    with db.begin() as conn:
        found = live.problems(conn, settings, system, spec, utcnow())
    db.dispose()
    typer.echo(f"design {system.design_hash} ({system.strategy.version}); caps {spec.caps.digest}")
    for name, value in spec.caps.model_dump().items():
        typer.echo(f"  {name}: {value:,}")
    if not found:
        typer.echo("live trading MAY start: every lock is open")
        return
    typer.echo("live trading may NOT start:")
    for problem in found:
        typer.echo(f"  - {problem}")


@gate_app.command("approve")
def gate_approve(config: LiveConfig = None) -> None:
    """Approve live trading of the current design under the current caps.
    For a person at an interactive terminal only."""
    from gats.oms import gate

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        typer.echo(
            "REFUSED: this needs an interactive terminal. A person has to read the limits and "
            "type the confirmation; it cannot be scripted or piped."
        )
        raise typer.Exit(1)
    settings = _settings()
    spec, system = _live_system(settings, config)
    report = settings.g3_report_path
    typer.echo(f"Design:   {system.design_hash} ({system.strategy.version})")
    typer.echo(f"G3 paper report: {report} says {gate.g3_verdict(report) or 'nothing'}")
    typer.echo("Real money at risk under this approval:")
    typer.echo(f"  most capital deployed at once: Rs {spec.caps.max_capital_rs:,.0f}")
    typer.echo(
        f"  loss at which it switches itself off (per day): Rs {spec.caps.max_daily_loss_rs:,.0f}"
    )
    typer.echo(f"  most in one stock: Rs {spec.caps.max_position_rs:,.0f}")
    typer.echo(f"  most real orders per day: {spec.caps.max_orders_per_day}")
    typer.echo(f"  valid for {spec.approval_valid_days} days")
    phrase = gate.confirmation(system.design_hash, spec.caps)
    typer.echo(f"To approve, type exactly:\n  {phrase}")
    typed = typer.prompt("confirmation", default="", show_default=False)
    db = make_engine(settings.resolved_db_url)
    init_db(db)
    try:
        with db.begin() as conn:
            approval = gate.approve(
                conn,
                design_hash=system.design_hash,
                caps=spec.caps,
                report=report,
                typed=typed,
                now=utcnow(),
                valid_days=spec.approval_valid_days,
            )
    except gate.ApprovalRefused as exc:
        typer.echo(f"REFUSED: {exc}")
        raise typer.Exit(1) from exc
    finally:
        db.dispose()
    typer.echo(f"approval #{approval} recorded. Revoke it with `gats gate revoke {approval}`.")


@gate_app.command("revoke")
def gate_revoke(
    approval: Annotated[int, typer.Argument(help="The approval's number.")],
    reason: Annotated[str, typer.Option(help="Why.")] = "revoked by hand",
) -> None:
    """Withdraw an approval: live trading cannot start under it again."""
    from gats.oms import gate

    settings = _settings()
    db = make_engine(settings.resolved_db_url)
    init_db(db)
    with db.begin() as conn:
        done = gate.revoke(conn, approval, reason, utcnow())
    db.dispose()
    typer.echo(f"approval #{approval} revoked" if done else f"no approval #{approval} in force")
    if not done:
        raise typer.Exit(1)


@app.command()
def live(
    name: Annotated[str, typer.Option(help="The run's name.")],
    config: LiveConfig = None,
) -> None:
    """Trade real money. HUMAN ONLY. Refuses unless live trading is switched
    on, the caps are set, gate G3 passed and a person approved this design."""
    from gats.alerts import alerter
    from gats.oms import gate
    from gats.runtime import live as live_runtime
    from gats.runtime.journal import DesignChanged, Diverged
    from gats.runtime.paper import PaperRuntime

    settings = _settings()
    spec, system = _live_system(settings, config)
    db = make_engine(settings.resolved_db_url)
    init_db(db)
    with db.begin() as conn:
        found = live_runtime.problems(conn, settings, system, spec, utcnow())
        approval = gate.approval_id(conn, system.design_hash, spec.caps)
    db.dispose()
    if found or approval is None:
        typer.echo("REFUSED: live trading may not start:")
        for problem in found:
            typer.echo(f"  - {problem}")
        raise typer.Exit(1)

    async def main() -> int:
        stop = asyncio.Event()
        if sys.platform != "win32":
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, stop.set)
        async with _services(settings) as svc:
            try:
                runtime = PaperRuntime(svc, system, name)
            except (DesignChanged, Diverged) as exc:
                typer.echo(f"REFUSED: {exc}")
                return 1
            oms = live_runtime.build_oms(svc, runtime, spec, approval)
            typer.echo(
                f"LIVE run {name!r}: design {system.design_hash}, approval #{approval}, capital "
                f"cap Rs {spec.caps.max_capital_rs:,.0f}. Ctrl+C stops it; creating "
                f"{oms.kill_switch} stops new entries."
            )
            await live_runtime.run_live(svc, runtime, oms, stop, alerter(svc))
        return 0

    try:
        code = asyncio.run(main())
    except KeyboardInterrupt:
        typer.echo("live run stopped")
        return
    raise typer.Exit(code)


@paper_app.command("report")
def paper_report(
    name: Annotated[str, typer.Option(help="The paper run to judge.")],
    criteria: Annotated[Path, typer.Option(help="The G3 criteria.")] = Path("configs/g3.yaml"),
    out: Annotated[Path | None, typer.Option(help="Where to write the report.")] = None,
) -> None:
    """Write the G3 report: the paper run against the backtest of the same
    filings and bars, judged by criteria fixed before the run."""
    from gats.runtime import g3
    from gats.runtime.journal import DesignChanged, Diverged
    from gats.runtime.paper import load_system
    from gats.runtime.status import runs

    settings = _settings()
    try:
        spec = g3.load_spec(criteria)
        system = load_system(settings.paper_config_path, settings)
    except (OSError, ValueError) as exc:
        typer.echo(f"cannot load the G3 criteria or the paper system: {exc}")
        raise typer.Exit(1) from exc
    db = make_engine(settings.resolved_db_url)
    init_db(db)
    try:
        with db.begin() as conn:
            if name not in {r.name for r in runs(conn)}:
                typer.echo(f"no paper run called {name!r}")
                raise typer.Exit(1)
            text, checks = g3.build_report(conn, system, name, spec, utcnow())
    except (DesignChanged, Diverged) as exc:
        typer.echo(f"REFUSED: {exc}")
        raise typer.Exit(1) from exc
    finally:
        db.dispose()
    path = out or settings.g3_report_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    for check in checks:
        typer.echo(f"  {'pass' if check.passed else 'FAIL'}  {check.name}: {check.detail}")
    typer.echo(f"G3: {g3.verdict(checks)} -> {path} (the decision at the gate is the human's)")
