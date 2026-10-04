"""Intraday reaction study (T5.3): entries after latency, exits, costs, guards.

Bars here are SYNTHETIC. The config is the real, pre-registered one.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import Connection, Engine

from gats.backtest.costs import CostModel, Fill
from gats.db import repo
from gats.marketdata.bars import write_month
from gats.marketdata.windows import EventWindow
from gats.pit import AsOf
from gats.rawstore import RawStore
from gats.refdata.calendar import TradingCalendar
from gats.research.reaction import (
    EntryRule,
    Horizon,
    NotReady,
    ReactionConfig,
    cost_fraction,
    measured_delay,
    react,
    run_reaction_study,
    verify_reaction,
)
from gats.research.study import RegistrationError
from gats.sources.models import AnnouncementRecord
from gats.sources.upstox import Bar
from gats.timeutil import ist_datetime
from tests.test_event_study import VERSION, Market

ROOT = Path(__file__).parents[1]
CONFIG = ROOT / "configs" / "studies" / "m5_reaction.yaml"
PREREG = ROOT / "docs" / "research" / "M5_prereg.md"
COSTS = CostModel.load(ROOT / "configs" / "costs" / "india_equity.yaml")
NOW = datetime(2024, 4, 1, tzinfo=UTC)
KEY, INDEX = "NSE_EQ|INE00000A0101", "NSE_INDEX|Nifty 500"
DAY = date(2024, 1, 10)
CFG, DIGEST = verify_reaction(CONFIG, PREREG)


def at(hh: int, mm: int, day: date = DAY, ss: int = 0) -> datetime:
    return ist_datetime(day, time(hh, mm, ss))


def session_bars(key: str, day: date, price: float = 100.0, step: float = 0.01,
                 skip: Sequence[tuple[int, int]] = ()) -> list[Bar]:  # fmt: skip
    """09:15 to 15:29, the open rising by ``step`` a minute; ``skip`` drops minutes."""
    bars = []
    first = at(9, 15, day)
    for i in range(375):
        ts = first + timedelta(minutes=i)
        local = (ts + timedelta(hours=5, minutes=30)).time()
        if (local.hour, local.minute) in skip:
            continue
        o = price + step * i
        bars.append(Bar(key, ts, o, o + 0.05, o - 0.05, o + step, 10_000, 0))
    return bars


def window(available: datetime, sessions: tuple[date, date, date] | None = None) -> EventWindow:
    days = sessions or (date(2024, 1, 9), DAY, date(2024, 1, 11))
    return EventWindow(1, "ORDER_WIN", 7, KEY, available, days[1], days)


@pytest.fixture
def cal(engine: Engine, store: RawStore) -> Iterator[TradingCalendar]:
    with engine.begin() as conn:
        market = Market(conn, store)
        market.list_stocks(["AAA"])
        market.write_prices(["AAA"])
        yield AsOf(conn, NOW).calendar()


def open_at(bars: Sequence[Bar], moment: datetime) -> float:
    return next(b.open for b in bars if b.ts == moment)


class TestConfig:
    def test_the_registered_config_is_the_one_on_disk(self, tmp_path: Path) -> None:
        assert CFG.study == "m5-intraday-reaction" and len(DIGEST) == 64
        assert [x.name for x in CFG.exits] == ["m5", "m15", "m30", "m60", "close"]
        changed = tmp_path / "m5_reaction.yaml"
        changed.write_text(
            CONFIG.read_text(encoding="utf-8").replace("minutes: 60", "minutes: 45"), "utf-8"
        )
        with pytest.raises(RegistrationError, match="not the pre-registered config"):
            verify_reaction(changed, PREREG)

    def test_amendment_1_order_wins_filed_in_market_hours(self, tmp_path: Path) -> None:
        assert CFG.events.confirmatory == ["ORDER_WIN"] and CFG.events.exploratory == []
        assert CFG.entry.confirmatory_strata == ["session"]
        assert CFG.entry.latency_percentile == 95
        with pytest.raises(ValidationError):  # a study must say which filings can pass
            EntryRule.model_validate(CFG.entry.model_dump() | {"confirmatory_strata": []})
        # The same design under another file name is not a registered study.
        copy = tmp_path / "m5.yaml"
        copy.write_bytes(CONFIG.read_bytes())
        with pytest.raises(RegistrationError, match=r"records no SHA-256 for m5\.yaml"):
            verify_reaction(copy, PREREG)

    def test_an_exit_is_minutes_or_a_named_moment(self) -> None:
        with pytest.raises(ValidationError):
            Horizon(name="x")
        with pytest.raises(ValidationError):
            Horizon(name="x", minutes=5, at="square_off")


class TestOneEvent:
    def test_entry_waits_for_the_delay_and_exits_are_bar_opens(self, cal: TradingCalendar) -> None:
        stock, index = session_bars(KEY, DAY), session_bars(INDEX, DAY, 1000.0, 0.0)
        row = react(window(at(11, 0)), stock, index, CFG, cal, 45.0, COSTS)
        assert row["filter_reason"] is None and row["stratum"] == "session"
        assert row["entry_at"] == at(11, 1)  # decided 11:00:45: the 11:00 bar had started
        entry = open_at(stock, at(11, 1))
        assert row["entry_price"] == entry
        for name, moment in [
            ("m1", at(11, 2)),
            ("m5", at(11, 6)),
            ("m60", at(12, 1)),
            ("close", at(15, 20)),
        ]:
            assert row[f"gross_{name}"] == pytest.approx(open_at(stock, moment) / entry - 1)
            assert row[f"abnormal_{name}"] == pytest.approx(row[f"gross_{name}"])  # flat index
        fee = cost_fraction(COSTS, entry, open_at(stock, at(11, 6)), 50_000, 5, DAY)
        assert row["net_m5"] == pytest.approx(row["abnormal_m5"] - fee)
        assert row["net0_m5"] > row["net_m5"] > row["net10_m5"]  # slippage sensitivity
        assert row["net0_m5"] - row["net10_m5"] == pytest.approx(0.002)

    def test_an_exit_past_the_square_off_does_not_exist(self, cal: TradingCalendar) -> None:
        stock, index = session_bars(KEY, DAY), session_bars(INDEX, DAY, 1000.0, 0.0)
        row = react(window(at(14, 40)), stock, index, CFG, cal, 45.0, COSTS)
        assert "gross_m60" not in row and "gross_m30" in row and "gross_close" in row

    @pytest.mark.parametrize(
        ("available", "entry_day"),
        [
            (at(15, 5), date(2024, 1, 11)),  # after the entry cutoff
            (at(18, 0), date(2024, 1, 11)),  # after hours
            (at(8, 30), DAY),  # before the open: today's first bar
            (at(18, 0, date(2024, 1, 25)), date(2024, 1, 29)),  # Republic Day and a weekend
        ],
    )
    def test_outside_trading_time_enters_at_the_next_open(
        self, cal: TradingCalendar, available: datetime, entry_day: date
    ) -> None:
        stock, index = session_bars(KEY, entry_day), session_bars(INDEX, entry_day, 1000.0, 0.0)
        row = react(window(available), stock, index, CFG, cal, 45.0, COSTS)
        assert (row["stratum"], row["entry_at"]) == ("overnight", at(9, 15, entry_day))

    def test_no_bar_soon_enough_drops_the_event(self, cal: TradingCalendar) -> None:
        quiet = [(11, m) for m in range(1, 12)]
        stock, index = session_bars(KEY, DAY, skip=quiet), session_bars(INDEX, DAY, 1000.0, 0.0)
        assert (
            react(window(at(11, 0)), stock, index, CFG, cal, 45.0, COSTS)["filter_reason"]
            == "no_bar"
        )
        assert (
            react(window(at(11, 0)), [], index, CFG, cal, 45.0, COSTS)["filter_reason"] == "no_bar"
        )

    def test_a_flat_bar_at_the_days_high_is_a_locked_circuit(self, cal: TradingCalendar) -> None:
        stock = session_bars(KEY, DAY)
        i = next(n for n, b in enumerate(stock) if b.ts == at(11, 1))
        top = max(b.high for b in stock[:i]) + 1
        stock[i] = Bar(KEY, at(11, 1), top, top, top, top, 50, 0)
        index = session_bars(INDEX, DAY, 1000.0, 0.0)
        assert (
            react(window(at(11, 0)), stock, index, CFG, cal, 45.0, COSTS)["filter_reason"]
            == "locked_entry"
        )

    def test_a_session_event_carries_its_previous_session_baseline(
        self, cal: TradingCalendar
    ) -> None:
        eve = date(2024, 1, 9)
        before = session_bars(KEY, eve, 200.0, 0.05)  # another day, another drift
        stock = before + session_bars(KEY, DAY)
        index = session_bars(INDEX, eve, 1000.0, 0.0) + session_bars(INDEX, DAY, 1000.0, 0.0)
        row = react(window(at(11, 0)), stock, index, CFG, cal, 45.0, COSTS)
        started = open_at(before, at(11, 1, eve))  # the entry's clock time, a session earlier
        for name, moment in [
            ("m5", at(11, 6, eve)),
            ("m60", at(12, 1, eve)),
            ("close", at(15, 20, eve)),
        ]:
            assert row[f"placebo_{name}"] == pytest.approx(open_at(before, moment) / started - 1)
        assert row["placebo_m5"] != pytest.approx(row["abnormal_m5"])
        assert "placebo_m1" not in row  # confirmatory exits only
        # No bars for the session before: the event stands, without a baseline.
        alone = react(window(at(11, 0)), session_bars(KEY, DAY), index, CFG, cal, 45.0, COSTS)
        assert alone["filter_reason"] is None and "placebo_m5" not in alone
        # A filing entered at the next open has no same-time baseline.
        late = react(window(at(18, 0, eve)), stock, index, CFG, cal, 45.0, COSTS)
        assert late["stratum"] == "overnight" and "placebo_m5" not in late

    def test_the_market_move_is_taken_out(self, cal: TradingCalendar) -> None:
        stock = session_bars(KEY, DAY)
        index = session_bars(INDEX, DAY, 1000.0, 0.5)  # the index rises too
        row = react(window(at(11, 0)), stock, index, CFG, cal, 45.0, COSTS)
        market = open_at(index, at(11, 6)) / open_at(index, at(11, 1)) - 1
        assert row["abnormal_m5"] == pytest.approx(row["gross_m5"] - market)
        assert (
            react(window(at(11, 0)), stock, [], CFG, cal, 45.0, COSTS)["filter_reason"]
            == "no_benchmark"
        )


class TestNoLookAhead:
    def test_a_longer_delay_never_enters_earlier(self, cal: TradingCalendar) -> None:
        stock, index = session_bars(KEY, DAY), session_bars(INDEX, DAY, 1000.0, 0.0)
        entries = [
            react(window(at(11, 0)), stock, index, CFG, cal, delay, COSTS)["entry_at"]
            for delay in (0.0, 30.0, 59.0, 61.0, 150.0, 600.0)
        ]
        assert entries == sorted(entries) and entries[0] == at(11, 0) and entries[-1] == at(11, 10)

    def test_nothing_before_the_entry_bar_changes_a_return(self, cal: TradingCalendar) -> None:
        stock, index = session_bars(KEY, DAY), session_bars(INDEX, DAY, 1000.0, 0.2)
        before = react(window(at(11, 0)), stock, index, CFG, cal, 45.0, COSTS)

        def rewrite(bars: list[Bar]) -> list[Bar]:  # a different morning, same shape
            return [
                Bar(
                    b.instrument_key,
                    b.ts,
                    b.open * 0.9,
                    b.high * 0.9,
                    b.low * 0.9,
                    b.close * 0.9,
                    1,
                    0,
                )
                if b.ts < at(11, 1)
                else b
                for b in bars
            ]

        after = react(window(at(11, 0)), rewrite(stock), rewrite(index), CFG, cal, 45.0, COSTS)
        returns = [k for k in before if k.split("_")[0] in ("gross", "abnormal", "net")]
        assert returns and all(after[k] == before[k] for k in returns)


def test_cost_fraction_is_the_verified_model_plus_slippage() -> None:
    quantity = 500  # Rs 50,000 at Rs 100
    charges = (
        COSTS.charges(Fill("buy", "intraday", 100.0, quantity, DAY)).total
        + COSTS.charges(Fill("sell", "intraday", 101.0, quantity, DAY)).total
    )
    assert cost_fraction(COSTS, 100.0, 101.0, 50_000, 5, DAY) == pytest.approx(
        charges / 50_000 + 0.001
    )
    assert cost_fraction(COSTS, 100.0, 101.0, 50_000, 5, DAY, delivery=True) > cost_fraction(
        COSTS, 100.0, 101.0, 50_000, 5, DAY
    )  # delivery pays STT on both sides and DP charges
    assert 0.0015 < cost_fraction(COSTS, 100.0, 101.0, 50_000, 5, DAY) < 0.004


# --- measured latency -------------------------------------------------------------------


def live_filings(conn: Connection, store: RawStore, lags: Sequence[float]) -> None:
    page = repo.save_raw(conn, store, b"p", kind="t", source="NSE", url="u",
                         content_type=None, fetched_at=NOW)  # fmt: skip
    for n, lag in enumerate(lags):
        shown = NOW - timedelta(days=2, minutes=n)
        record = AnnouncementRecord(
            source="NSE", source_ann_id=f"live{n}", symbol="AAA", scrip_code=None, isin=None,
            company_name="A", category="x", subcategory=None, subject=None, details=None,
            attachment_url=None, exch_submitted_ts=None, exch_disseminated_ts=shown, event_ts=shown,
        )  # fmt: skip
        seen = shown + timedelta(seconds=lag)
        repo.insert_announcements(conn, [record], raw_doc_id=page, parser_version="v",
                                  mode="live", fetched_at=seen, now=seen)  # fmt: skip


def test_delay_is_measured_from_live_recording(engine: Engine, store: RawStore) -> None:
    cfg = CFG.model_copy(
        update={"entry": CFG.entry.model_copy(update={"latency_min_filings": 100})}
    )
    with engine.begin() as conn:
        with pytest.raises(NotReady, match="recorder must run"):
            measured_delay(conn, cfg, NOW)
        live_filings(conn, store, [float(n) for n in range(1, 101)])  # 1 s .. 100 s
        delay = measured_delay(conn, cfg, NOW)
        assert delay.filings == 100 and delay.feed_s == pytest.approx(95.05)
        assert delay.total_s == pytest.approx(95.05 + 20 + 5)
        assert measured_delay(conn, cfg, NOW, percentile=50).feed_s == pytest.approx(50.5)
        with pytest.raises(NotReady):
            measured_delay(conn, CFG, NOW)  # the registered minimum is 500 filings


# --- the study over a database -----------------------------------------------------------


def with_key(bars: Sequence[Bar], key: str) -> list[Bar]:
    return [Bar(key, b.ts, b.open, b.high, b.low, b.close, b.volume, b.open_interest) for b in bars]


def test_study_rows_filters_and_periods(engine: Engine, store: RawStore, tmp_path: Path) -> None:
    cfg: ReactionConfig = CFG.model_copy(update={"taxonomy_version": VERSION})
    with engine.begin() as conn:
        market = Market(conn, store, turnover_rs={"THIN": 2e6})
        market.list_stocks(["AAA", "THIN"])
        market.write_prices(["AAA", "THIN"])
        kept = market.filing("AAA", "ORDER_WIN", at(11, 0))
        again = market.filing("AAA", "ORDER_WIN", at(11, 0, date(2024, 1, 12)))  # within 5 sessions
        train = market.filing("AAA", "ORDER_WIN", at(11, 0, date(2023, 12, 20)))
        thin = market.filing("THIN", "ORDER_WIN", at(11, 0))
        market.filing(
            "AAA",
            "ORDER_WIN",
            at(11, 0, date(2024, 2, 1)),
            category="Daily Buy Back of equity shares",
        )
        clock = AsOf(conn, NOW)
        resolver = clock.resolver()

        def key_of(symbol: str) -> str:
            security = resolver.resolve("nse_symbol", symbol, DAY)
            assert security is not None
            return f"NSE_EQ|{resolver.identifier(security, 'isin', DAY)}"

        for day in (date(2023, 12, 20), DAY, date(2024, 1, 12)):
            month = day.replace(day=1)
            write_month(
                tmp_path,
                key_of("AAA"),
                month,
                with_key(session_bars(KEY, day, step=0.02), key_of("AAA")),
            )
            write_month(tmp_path, INDEX, month, session_bars(INDEX, day, 1000.0, 0.0))
        write_month(
            tmp_path,
            key_of("THIN"),
            DAY.replace(day=1),
            with_key(session_bars(KEY, DAY), key_of("THIN")),
        )
        found = run_reaction_study(
            clock, cfg, tmp_path, delay_s=45.0, scope=["ORDER_WIN"], costs=COSTS, index_key=INDEX
        )
    rows = {r["announcement_id"]: r for r in found}
    assert len(rows) == 4  # the buyback progress report was excluded before any price was read
    assert (rows[kept]["filter_reason"], rows[kept]["period"]) == (None, "test")
    assert rows[kept]["median_turnover_rs"] == pytest.approx(5e7)
    assert rows[kept]["gross_m60"] == pytest.approx(0.02 * 60 / rows[kept]["entry_price"])
    assert rows[again]["filter_reason"] == "duplicate"
    assert (rows[train]["filter_reason"], rows[train]["period"]) == (None, "train")
    assert rows[thin]["filter_reason"] == "illiquid"


# --- the decision and the report --------------------------------------------------------


def synthetic_rows(
    event_type: str, mean: float, n: int, seed: int, *, overnight_extra: float = 0.0
) -> list[dict[str, object]]:
    """Half the events are filed in the session, half overnight (those earn
    ``overnight_extra`` on top)."""
    import random

    rng = random.Random(seed)
    rows: list[dict[str, object]] = []
    for i in range(n):
        in_session = bool(i % 2)
        row: dict[str, object] = {
            "announcement_id": seed * 10_000 + i,
            "event_type": event_type,
            "filter_reason": None,
            "period": "test",
            "stratum": "session" if in_session else "overnight",
            "entry_date": date(2024, 1, 1) + timedelta(days=i % 90),
        }
        for x in CFG.all_exits:
            net = rng.gauss(mean + (0.0 if in_session else overnight_extra), 0.01)
            row |= {
                f"net_{x.name}": net,
                f"abnormal_{x.name}": net + 0.0023,
                f"gross_{x.name}": net,
            }
            row |= {f"net0_{x.name}": net + 0.001, f"net10_{x.name}": net - 0.001}
            if in_session:
                row[f"placebo_{x.name}"] = -0.001
        rows.append(row)
    return rows


def test_g1b_passes_a_planted_effect_and_not_noise_or_small_samples() -> None:
    from gats.research.reaction import Delay
    from gats.research.reaction_report import build_reaction_report, decide

    quick = CFG.model_copy(
        update={"statistics": CFG.statistics.model_copy(update={"bootstrap_resamples": 500})}
    )
    rows = (
        synthetic_rows("ORDER_WIN", 0.004, 400, 1)
        # Noise. The gate reads its 200 session filings; about one noise sample
        # in ten passes one of five exits by chance (seed 2 does, at m60: the
        # false discoveries BH allows), so this is a sample that does not.
        + synthetic_rows("PRESS_RELEASE", 0.0, 400, 8)
        + synthetic_rows("BUYBACK", 0.02, 40, 3)  # a large effect on too few events
    )
    rows.append({"announcement_id": 1, "event_type": "ORDER_WIN", "filter_reason": "illiquid",
                 "period": None, "entry_date": None})  # fmt: skip
    scope = ["ORDER_WIN", "PRESS_RELEASE", "BUYBACK"]
    _, family = decide(rows, quick, scope)  # type: ignore[arg-type]
    assert len(family) == 15  # 3 types x 5 confirmatory exits
    verdicts = {(c.event_type, c.exit): c for c in family}
    assert all(verdicts[("ORDER_WIN", x.name)].passes for x in CFG.exits)
    assert not any(verdicts[("PRESS_RELEASE", x.name)].passes for x in CFG.exits)
    small = verdicts[("BUYBACK", "m5")]  # only its 20 session filings count
    assert not small.passes and any("N_test 20 < 100" in r for r in small.reasons)

    text, _ = build_reaction_report(
        rows, quick, digest=DIGEST, run_id="r1", experiment_id=7, scope=scope,  # type: ignore[arg-type]
        delay=Delay(40.0, 900, 65.0), median_rows=rows, median_delay=Delay(8.0, 900, 33.0),  # type: ignore[arg-type]
        m3_run_date=date(2024, 2, 15),
    )  # fmt: skip
    assert "**G1b: PASS: ORDER_WIN at m5" in text
    assert "65.0 s = feed latency p95 40.0 s (measured on 900 live filings) + 20 s + 5 s" in text
    assert "| ORDER_WIN | illiquid | 1 |" in text and "| ORDER_WIN | kept | 400 |" in text
    assert "| ORDER_WIN | 10 bp slippage | m5 |" in text and "entries after 2024-02-15" in text
    assert "- Filings that can pass the gate: session (" in text
    assert "## Baseline: the same stock, the same time of day, one session earlier" in text
    baseline = next(line for line in text.splitlines() if "| -0.100% |" in line)
    assert baseline.startswith("| ORDER_WIN | m5 | 200 |")  # the session filings, paired
    nothing, _ = build_reaction_report(
        synthetic_rows("PRESS_RELEASE", 0.0, 400, 8), quick, digest=DIGEST, run_id="r2",  # type: ignore[arg-type]
        experiment_id=8, scope=["PRESS_RELEASE"], delay=Delay(40.0, 900, 65.0),
    )  # fmt: skip
    assert "**G1b: no tradeable remainder found.**" in nothing
    assert "M3 has no recorded run" in nothing


def test_only_filings_made_in_market_hours_can_pass() -> None:
    from gats.research.reaction_report import decide

    quick = CFG.model_copy(
        update={"statistics": CFG.statistics.model_copy(update={"bootstrap_resamples": 500})}
    )
    # A loss in the session and a large gain overnight: pooled, it would pass.
    rows = synthetic_rows("ORDER_WIN", -0.002, 400, 5, overnight_extra=0.03)
    _, family = decide(rows, quick, ["ORDER_WIN"])  # type: ignore[arg-type]
    assert len(family) == 5 and all(c.n == 200 for c in family)  # the overnight half is out
    assert not any(c.passes for c in family)
    pooled_rule = quick.entry.model_copy(update={"confirmatory_strata": ["session", "overnight"]})
    _, pooled = decide(rows, quick.model_copy(update={"entry": pooled_rule}), ["ORDER_WIN"])  # type: ignore[arg-type]
    assert all(c.n == 400 and c.passes for c in pooled)


def test_the_saved_frame_keeps_every_column(tmp_path: Path) -> None:
    import pyarrow.parquet as pq

    from gats.research.event_study import write_parquet

    rows = [
        {"announcement_id": 1, "filter_reason": "no_bar"},  # a dropped event comes first
        {"announcement_id": 2, "filter_reason": None, "net_m5": 0.01, "placebo_m5": -0.001},
    ]
    write_parquet(rows, tmp_path / "events.parquet")
    table = pq.read_table(tmp_path / "events.parquet")
    assert table.column_names == ["announcement_id", "filter_reason", "net_m5", "placebo_m5"]
    assert table.column("net_m5").to_pylist() == [None, 0.01]
