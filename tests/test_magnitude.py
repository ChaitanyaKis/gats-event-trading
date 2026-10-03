"""The order-magnitude study (T4.7): the registered filter and its report."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine

from gats.db.schema import extractions
from gats.pit import AsOf
from gats.rawstore import RawStore
from gats.research.event_study import run_event_study
from gats.research.magnitude import build_magnitude_report, size_filter, verify_magnitude
from gats.research.study import RegistrationError
from gats.timeutil import ist_datetime
from tests.test_event_study import VERSION, Market, after_close, config
from tests.test_reaction import synthetic_rows
from tests.test_results_ingest import CRORE, quarter

ROOT = Path(__file__).parents[1]
CONFIG = ROOT / "configs" / "studies" / "m4_magnitude.yaml"
PREREG = ROOT / "docs" / "research" / "M4_prereg.md"
NOW = datetime(2024, 4, 1, tzinfo=UTC)
CFG, DIGEST = verify_magnitude(CONFIG, PREREG)


def test_the_registered_design(tmp_path: Path) -> None:
    assert CFG.magnitude.min_amount_vs_revenue == 0.10 and CFG.events.confirmatory == ["ORDER_WIN"]
    assert CFG.statistics.fdr.q == 0.025  # the second study on M3's test period
    assert [x.name for x in CFG.exits] == ["d0", "d1", "d3", "d5"]
    edited = tmp_path / "m4.yaml"
    edited.write_text(CONFIG.read_text(encoding="utf-8").replace("0.10", "0.03"), "utf-8")
    with pytest.raises(RegistrationError):
        verify_magnitude(edited, PREREG)


def order(
    conn: Connection, market: Market, symbol: str, when: datetime, crore: float | None
) -> int:
    filing = market.filing(symbol, "ORDER_WIN", when)
    if crore is not None:
        conn.execute(
            extractions.insert().values(
                announcement_id=filing, extractor_version="cascade:test", event_type="ORDER_WIN",
                method="rules_annexure", fields={"amount_inr": crore * CRORE},
                amount_inr=crore * CRORE, confidence=0.9, created_at=NOW,
            )
        )  # fmt: skip
    return filing


def test_size_filter_reads_amount_and_revenue_as_of_the_event(
    engine: Engine, store: RawStore
) -> None:
    cfg = CFG.model_copy(update={"taxonomy_version": VERSION})
    with engine.begin() as conn:
        market = Market(conn, store)
        market.list_stocks(["AAA", "BBB"])
        market.write_prices(["AAA", "BBB"])
        doc = market.doc()
        for end, public in [
            (date(2023, 3, 31), date(2023, 5, 10)),
            (date(2023, 6, 30), date(2023, 8, 10)),
            (date(2023, 9, 30), date(2023, 11, 10)),
            (date(2023, 12, 31), date(2024, 2, 10)),
        ]:
            quarter(conn, doc, end, 100, public)  # AAA: Rs 400 crore a year once all are public
        big = order(conn, market, "AAA", after_close(date(2024, 2, 20)), 60)  # 15% of revenue
        small = order(conn, market, "AAA", after_close(date(2024, 3, 5)), 8)  # 2%
        unsized = order(conn, market, "AAA", after_close(date(2024, 3, 12)), None)
        early = order(conn, market, "AAA", after_close(date(2024, 1, 15)), 60)  # Q3 not public yet
        no_results = order(conn, market, "BBB", after_close(date(2024, 2, 20)), 60)
        clock = AsOf(conn, NOW)
        results = {
            e.announcement_id: e for e in run_event_study(clock, config(), size_filter(clock, cfg))
        }
        loose = {
            e.announcement_id: e
            for e in run_event_study(clock, config(), size_filter(clock, cfg, 0.01))
        }
    assert results[big].filter_reason is None and results[big].entry_date == date(2024, 2, 21)
    assert results[small].filter_reason == "below_magnitude"
    assert results[unsized].filter_reason == "no_amount"
    assert results[early].filter_reason == "no_revenue"  # three quarters were public, not four
    assert results[no_results].filter_reason == "no_revenue"
    assert loose[small].filter_reason is None  # 2% passes the 1% exploratory threshold


def test_without_a_filter_the_engine_is_m3s(engine: Engine, store: RawStore) -> None:
    with engine.begin() as conn:
        market = Market(conn, store)
        market.list_stocks(["AAA"])
        market.write_prices(["AAA"])
        filing = market.filing("AAA", "ORDER_WIN", ist_datetime(date(2024, 1, 10), time(16, 0)))
        (event,) = run_event_study(AsOf(conn, NOW), config())
    assert event.announcement_id == filing and event.filter_reason is None


def test_report_states_the_decision_and_the_second_use_of_the_holdout() -> None:
    quick = CFG.model_copy(
        update={"statistics": CFG.statistics.model_copy(update={"bootstrap_resamples": 400})}
    )

    def rows(mean: float, n: int, seed: int) -> list[dict[str, object]]:
        made = synthetic_rows("ORDER_WIN", mean, n, seed)
        for row in made:
            for x in CFG.exits:
                row[f"net_{x.name}"] = row["net_m5"]
                row[f"abnormal_{x.name}"] = row["abnormal_m5"]
        return made

    kept = rows(0.01, 300, 1)
    dropped: dict[str, object] = {"announcement_id": 9, "event_type": "ORDER_WIN"}
    kept.append(dropped | {"filter_reason": "below_magnitude", "period": None, "entry_date": None})
    others = {0.01: rows(0.0, 300, 2), 0.20: rows(0.02, 120, 3)}
    text, family = build_magnitude_report(
        kept,  # type: ignore[arg-type]
        others,  # type: ignore[arg-type]
        quick,
        digest=DIGEST,
        run_id="r1",
        experiment_id=4,
        accuracy_note="cascade, 280, 93.2% (89.6% to 95.6%)",
    )
    assert len(family) == 4 and all(c.passes for c in family)
    assert "**Result: PASS at d0, d1, d3, d5.**" in text
    assert "BH at q = 0.025" in text and "93.2%" in text
    assert "| >= 1% | d0 | 300 |" in text and "| >= 20% | d5 | 120 |" in text
    assert "| below_magnitude | 1 |" in text and "| kept | 300 |" in text
    nothing, family = build_magnitude_report(
        rows(0.0, 300, 4), {}, quick, digest=DIGEST, run_id="r2", experiment_id=5,  # type: ignore[arg-type]
        accuracy_note="n/a",
    )  # fmt: skip
    assert "size does not help (no pass)" in nothing and not any(c.passes for c in family)
