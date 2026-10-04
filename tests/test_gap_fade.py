"""M5's exploratory arm B, the gap fade: who is an event, who is a control,
and that the filing effect is the event minus its matched controls. The
market is SYNTHETIC: hand-made daily rows that isolate each rule."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Connection, Engine, update

from gats.backtest.costs import CostModel
from gats.db import repo
from gats.db.schema import eod_prices, price_bands
from gats.pit import AsOf
from gats.rawstore import RawStore
from gats.refdata.actions import store_actions
from gats.research.gap_fade import (
    GapDay,
    band_fraction,
    build_gap_fade_report,
    control_records,
    run_gap_fade,
    short_cost,
    supported,
    verify_gap_fade,
)
from gats.research.study import RegistrationError, registered_hash_for
from gats.sources.models import EodRecord
from gats.sources.nse_corp_actions import CorporateActionRecord
from gats.timeutil import ist_datetime
from tests.test_event_study import VERSION, Market

ROOT = Path(__file__).parents[1]
CONFIG = ROOT / "configs" / "studies" / "m5b_gap_fade.yaml"
PREREG = ROOT / "docs" / "research" / "M5_prereg.md"
COSTS = CostModel.load(ROOT / "configs" / "costs" / "india_equity.yaml")
CFG, DIGEST = verify_gap_fade(CONFIG, PREREG)
NOW = datetime(2024, 4, 1, tzinfo=UTC)
DAY = date(2024, 2, 6)  # a Tuesday; the gap day
EVE = date(2024, 2, 5)
OVERNIGHT = ist_datetime(EVE, time(18, 0))


def weekdays(first: date, last: date) -> list[date]:
    days, day = [], first
    while day <= last:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def prices(
    market: Market,
    symbol: str,
    *,
    gap: float,
    fade: float,
    turnover_rs: float = 5e7,
    flat: bool = False,
) -> None:
    """Thirty quiet sessions at 100, then DAY opens ``gap`` up and closes
    ``fade`` below its open."""
    rows = []
    for day in weekdays(date(2023, 12, 20), EVE):
        rows.append(
            EodRecord(day, symbol, "EQ", 100.0, 100.0, 100.5, 99.5, 100.0, 100.0, 100.0, 1000,
                      turnover_rs / 1e5, 100, 500, 50.0)
        )  # fmt: skip
    open_ = 100.0 * (1 + gap)
    close = open_ * (1 - fade)
    high, low = (open_, open_) if flat else (max(open_, close) + 0.5, min(open_, close) - 0.5)
    rows.append(
        EodRecord(DAY, symbol, "EQ", 100.0, open_, high, low, close, close, close, 1000,
                  turnover_rs / 1e5, 100, 500, 50.0)
    )  # fmt: skip
    repo.upsert_eod(
        market.conn, rows, raw_doc_id=market.doc(), parser_version="v",
        available_at=datetime(2024, 3, 1, tzinfo=UTC),
    )  # fmt: skip


def study(conn: Connection) -> tuple[dict[str, dict[str, Any]], dict[Any, list[float]]]:
    """(the study's events by symbol, its controls' short returns by cell)."""
    cfg = CFG.model_copy(update={"taxonomy_version": VERSION})
    events, found = run_gap_fade(AsOf(conn, NOW), cfg, COSTS)
    controls = {cell: [g.short_gross for g in members] for cell, members in found.items()}
    resolver = AsOf(conn, NOW).resolver()
    by_symbol = {}
    for event in events:
        symbol = resolver.identifier(event["security_id"], "nse_symbol", DAY)
        assert symbol is not None and symbol not in by_symbol  # one filing per stock here
        by_symbol[symbol] = event
    return by_symbol, controls


def test_the_registered_design(tmp_path: Path) -> None:
    assert CFG.study == "m5b-gap-fade" and CFG.event_types == ["ORDER_WIN"]
    assert (CFG.min_gap, CFG.band_margin, CFG.gap_bins[-1]) == (0.01, 0.01, 0.195)
    assert CFG.latest_filing_ist == time(9, 0)
    assert registered_hash_for(PREREG, CONFIG) == DIGEST
    assert registered_hash_for(PREREG, Path("m5_reaction.yaml")) != DIGEST  # each config its own
    changed = tmp_path / "m5b_gap_fade.yaml"
    changed.write_text(CONFIG.read_text("utf-8").replace("min_gap: 0.01", "min_gap: 0.02"), "utf-8")
    with pytest.raises(RegistrationError, match="not the pre-registered config"):
        verify_gap_fade(changed, PREREG)
    with pytest.raises(RegistrationError, match="records no SHA-256"):
        registered_hash_for(PREREG, Path("something_else.yaml"))


def test_the_command_refuses_a_changed_design_and_incomplete_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from gats.cli import app

    monkeypatch.setenv("GATS_DATA_DIR", str(tmp_path / "data"))
    report = tmp_path / "report.md"
    changed = tmp_path / "m5b_gap_fade.yaml"
    changed.write_text(CONFIG.read_text("utf-8").replace("min_gap: 0.01", "min_gap: 0.02"), "utf-8")
    args = ["research", "gap-fade", "--prereg", str(PREREG), "--report", str(report)]
    result = CliRunner().invoke(app, [*args, "--config", str(changed)])
    assert result.exit_code == 1 and "not the pre-registered config" in result.stdout
    # The registered design, on an empty database: no holdout is spent on it.
    result = CliRunner().invoke(app, [*args, "--config", str(CONFIG)])
    assert result.exit_code == 1 and "REFUSED: the data is not complete" in result.stdout
    assert not report.exists()


def test_a_filing_gap_is_measured_against_gaps_without_a_filing(
    engine: Engine, store: RawStore
) -> None:
    with engine.begin() as conn:
        market = Market(conn, store)
        market.list_stocks(["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH", "III"])
        prices(market, "AAA", gap=0.035, fade=0.02)  # the event: opens +3.5%, gives back 2%
        market.filing("AAA", "ORDER_WIN", OVERNIGHT)
        prices(market, "BBB", gap=0.04, fade=0.005)  # same bin and bucket, no filing: a control
        prices(market, "CCC", gap=0.032, fade=0.007)  # another control
        prices(market, "DDD", gap=0.04, fade=0.03)  # filed something else overnight: not one
        market.filing("DDD", "MGMT_CHANGE", OVERNIGHT)
        prices(market, "EEE", gap=0.04, fade=0.03, turnover_rs=5e8)  # another liquidity bucket
        prices(market, "FFF", gap=0.07, fade=0.03)  # another gap bin
        prices(market, "GGG", gap=0.005, fade=0.0)  # an order win that did not gap up
        market.filing("GGG", "ORDER_WIN", OVERNIGHT)
        prices(market, "HHH", gap=0.035, fade=0.02)  # filed during the session: arm A's
        market.filing("HHH", "ORDER_WIN", ist_datetime(DAY, time(11, 0)))
        prices(market, "III", gap=0.035, fade=0.02, flat=True)  # locked: cannot be traded
        market.filing("III", "ORDER_WIN", OVERNIGHT)
        events, controls = study(conn)
        cfg = CFG.model_copy(update={"taxonomy_version": VERSION})
        saved = control_records(run_gap_fade(AsOf(conn, NOW), cfg, COSTS)[1])

    assert set(events) == {"AAA", "GGG", "HHH", "III"}  # the order wins, and only they
    event = events["AAA"]
    assert event["filter_reason"] is None and event["entry_date"] == DAY
    assert event["period"] == "test" and event["same_morning"] is False
    assert event["gap"] == pytest.approx(0.035) and event["short_gross"] == pytest.approx(0.02)
    assert (event["bucket"], event["bin"]) == (0, 2)  # Rs 1-10 crore; the 3-5% gap bin
    # DDD and HHH filed around the session; EEE and FFF are in other cells.
    assert controls == {(DAY, 0, 2): [pytest.approx(0.005), pytest.approx(0.007)]}
    assert event["controls"] == 2 and event["control_gross"] == pytest.approx(0.006)
    # The frame a run saves names each control, so it can be checked by hand.
    assert [(r["symbol"], r["entry_date"], r["bin"]) for r in saved] == [
        ("BBB", DAY, 2),
        ("CCC", DAY, 2),
    ]
    assert saved[0]["short_gross"] == pytest.approx(0.005) and saved[0]["open"] == 104.0
    assert event["effect"] == pytest.approx(0.014)  # the filing's own share of the fade
    fee = short_cost(COSTS, GapDay("AAA", DAY, 0.035, 0, 2, 103.5, 103.5 * 0.98), CFG)
    assert 0.0015 < fee < 0.004 and event["short_net"] == pytest.approx(0.02 - fee)
    assert events["GGG"]["filter_reason"] == "no_gap_up"
    assert events["III"]["filter_reason"] == "locked"
    assert events["HHH"]["filter_reason"] == "in_session" and "short_gross" not in events["HHH"]


def test_a_filing_must_be_public_before_the_opening_auction(
    engine: Engine, store: RawStore
) -> None:
    with engine.begin() as conn:
        market = Market(conn, store)
        market.list_stocks(["AAA", "BBB", "CCC", "DDD", "EEE"])
        for symbol in ("AAA", "BBB", "CCC", "DDD", "EEE"):
            prices(market, symbol, gap=0.035, fade=0.02)
        market.filing("AAA", "ORDER_WIN", ist_datetime(DAY, time(8, 30)))
        market.filing("BBB", "ORDER_WIN", ist_datetime(DAY, time(9, 0)))  # at 09:00:00 exactly
        # No second in the timestamp: it may have appeared at 09:00:59.
        market.filing("CCC", "ORDER_WIN", ist_datetime(DAY, time(9, 0)), disseminated=False)
        market.filing("DDD", "ORDER_WIN", ist_datetime(DAY, time(9, 10)))  # after the auction
        market.filing("EEE", "ORDER_WIN", ist_datetime(date(2024, 2, 3), time(12, 0)))  # Saturday
        events, controls = study(conn)
    assert (events["AAA"]["filter_reason"], events["AAA"]["same_morning"]) == (None, True)
    assert events["BBB"]["filter_reason"] is None
    assert events["CCC"]["filter_reason"] == "too_late"
    assert events["DDD"]["filter_reason"] == "too_late" and events["DDD"]["entry_date"] == DAY
    # A weekend filing trades on the Monday, which did not gap; not on DAY.
    assert events["EEE"]["entry_date"] == EVE and events["EEE"]["filter_reason"] == "no_gap_up"
    # A company that filed too late for the open still filed: it is no control.
    assert controls == {(DAY, 0, 2): [pytest.approx(0.02)]}  # EEE, which filed for Monday
    assert events["AAA"]["controls"] == 1 and events["AAA"]["effect"] == pytest.approx(0.0)


def test_no_short_near_the_upper_band_or_on_an_ex_date(engine: Engine, store: RawStore) -> None:
    with engine.begin() as conn:
        market = Market(conn, store)
        market.list_stocks(["AAA", "BBB", "CCC", "DDD"])
        for symbol in ("AAA", "BBB", "CCC", "DDD"):
            prices(market, symbol, gap=0.042, fade=0.02)
            market.filing(symbol, "ORDER_WIN", OVERNIGHT)
        for symbol, band in (("AAA", "5"), ("BBB", "20"), ("CCC", "No Band")):
            conn.execute(
                price_bands.insert().values(
                    as_of_date=date(2024, 3, 28), symbol=symbol, series="EQ", band=band,
                    available_at=datetime(2024, 3, 28, tzinfo=UTC), raw_doc_id=market.doc(),
                    parser_version="v",
                )
            )  # fmt: skip
        store_actions(
            conn,
            [
                CorporateActionRecord("DDD", "EQ", None, None, "Dividend - Rs 2 Per Share", DAY,
                                      None, 10.0, "DIVIDEND", None, 2.0, False)
            ],
            fetched_at=datetime(2024, 1, 15, tzinfo=UTC),
            raw_doc_id=market.doc(),
            parser_version="v",
        )  # fmt: skip
        events, _ = study(conn)
    # A 5% band: +4.2% is within one point of the limit (+4% and beyond is refused).
    assert events["AAA"]["filter_reason"] == "near_band"
    assert events["BBB"]["filter_reason"] is None  # a 20% band is far away
    assert events["CCC"]["filter_reason"] is None  # no band: no limit to be near
    assert events["DDD"]["filter_reason"] == "ex_date"  # its previous close is not comparable
    assert events["BBB"]["controls"] == 0 and "effect" not in events["BBB"]  # everyone filed
    assert [band_fraction(b) for b in ("5", "20", "No Band", None)] == [0.05, 0.2, None, None]


def test_nothing_the_clock_has_not_seen_is_used(engine: Engine, store: RawStore) -> None:
    """Leak test: prices and filings that arrive after the clock do not exist,
    and the gap day's own trading is not in its liquidity filter."""
    cfg = CFG.model_copy(update={"taxonomy_version": VERSION})
    with engine.begin() as conn:
        market = Market(conn, store)
        market.list_stocks(["AAA", "BBB", "CCC"])
        prices(market, "AAA", gap=0.035, fade=0.02)  # these rows arrive on 2024-03-01
        prices(market, "BBB", gap=0.04, fade=0.005)
        prices(market, "CCC", gap=0.035, fade=0.02, turnover_rs=2e6)  # thin until DAY ...
        conn.execute(
            update(eod_prices)
            .where(eod_prices.c.symbol == "CCC", eod_prices.c.trade_date == DAY)
            .values(turnover_lacs=1e6)  # ... and very busy on it
        )
        market.filing("AAA", "ORDER_WIN", OVERNIGHT)
        market.filing("CCC", "ORDER_WIN", OVERNIGHT)

        before_prices = AsOf(conn, datetime(2024, 2, 20, tzinfo=UTC))
        events, found = run_gap_fade(before_prices, cfg, COSTS)
        assert [e["filter_reason"] for e in events] == ["no_price", "no_price"]
        assert found == {}
        before_filing = AsOf(conn, datetime(2024, 2, 1, tzinfo=UTC))
        assert run_gap_fade(before_filing, cfg, COSTS) == ([], {})
        events, controls = study(conn)
    assert events["AAA"]["filter_reason"] is None and events["AAA"]["controls"] == 1
    assert events["CCC"]["filter_reason"] == "illiquid"
    assert controls == {(DAY, 0, 2): [pytest.approx(0.005)]}


def test_the_report_says_exploratory_and_judges_the_filing_effect() -> None:
    quick = CFG.model_copy(
        update={"statistics": CFG.statistics.model_copy(update={"bootstrap_resamples": 500})}
    )

    def events(effect: float, net: float, n: int = 150) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for i in range(n):
            wobble = 0.004 * ((i % 5) - 2) / 2  # averages to zero over five events
            rows.append(
                {
                    "announcement_id": i, "filter_reason": None, "period": "test",
                    "entry_date": date(2024, 1, 1) + timedelta(days=i % 60),
                    "same_morning": i % 3 == 0, "short_gross": net + 0.002 + wobble,
                    "short_net": net + wobble, "controls": 3, "control_gross": 0.003,
                    "effect": effect + wobble,
                }
            )  # fmt: skip
        rows.append({"announcement_id": 999, "filter_reason": "no_gap_up", "period": "test"})
        return rows

    ok, missing = supported(events(0.006, 0.004), quick)
    assert ok and missing == []
    text = build_gap_fade_report(
        events(0.006, 0.004), quick, digest=DIGEST, run_id="r1", experiment_id=3
    )
    assert "**Exploratory result: supported on history: worth a forward test" in text
    assert "this history cannot confirm it" in text
    assert "| Filing effect (event minus its controls) | 150 | +0.60% |" in text
    assert "| no_gap_up | 1 |" in text and "| kept | 150 |" in text

    ok, missing = supported(events(0.0, 0.004), quick)  # it fades, but so do the controls
    assert not ok and missing == ["the filing adds nothing beyond its matched controls"]
    ok, missing = supported(events(0.006, -0.001), quick)  # the filing adds, costs eat it
    assert not ok and len(missing) == 1 and "does not earn after costs" in missing[0]
    ok, missing = supported(events(0.006, 0.004, n=40), quick)
    assert not ok and "only 40 matched events" in missing[0]
