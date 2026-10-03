"""The verified cost model (T6.1): the real rate file, the arithmetic, the guards."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from gats.backtest.costs import CostModel, Fill, UnverifiedPeriod

CONFIG = Path(__file__).parents[1] / "configs" / "costs" / "india_equity.yaml"
CRORE = 1e7


@pytest.fixture(scope="module")
def model() -> CostModel:
    return CostModel.load(CONFIG)


def test_every_value_cites_an_official_source(model: CostModel) -> None:
    assert model.version.startswith("india-equity-costs-v1+")
    for name in type(model.spec.components).model_fields:
        for period in getattr(model.spec.components, name):
            sources = [period.source] if isinstance(period.source, str) else period.source
            assert sources and all(s.startswith("https://") for s in sources), name
            assert period.checked <= date(2026, 10, 3)


# Rupees per crore of traded value, each side, as the circulars state them.
@pytest.mark.parametrize(
    ("component", "day", "per_crore"),
    [
        ("exchange_transaction", date(2021, 6, 1), 345.0),  # FA46730 slab 1
        ("exchange_transaction", date(2023, 6, 1), 325.0),  # FA56129 slab 1
        ("exchange_transaction", date(2024, 6, 1), 322.0),  # Upstox's client rate
        ("exchange_transaction", date(2025, 6, 1), 297.0),  # FA64232
        ("exchange_transaction", date(2026, 6, 1), 306.99),  # FA73061
        ("ipft", date(2022, 6, 1), 0.01),
        ("ipft", date(2024, 6, 1), 10.0),
        ("ipft", date(2026, 6, 1), 0.01),
        ("sebi_fee", date(2025, 6, 1), 10.0),
    ],
)
def test_rates_match_the_circulars(
    model: CostModel, component: str, day: date, per_crore: float
) -> None:
    for side in ("buy", "sell"):
        assert model.rate(component, "intraday", side, day) * CRORE == pytest.approx(per_crore)  # type: ignore[arg-type]


def test_statutory_rates(model: CostModel) -> None:
    assert model.rate("stt", "delivery", "buy") == model.rate("stt", "delivery", "sell") == 0.001
    assert (model.rate("stt", "intraday", "buy"), model.rate("stt", "intraday", "sell")) == (
        0.0,
        0.00025,
    )
    assert model.rate("stamp_duty", "delivery", "buy") * CRORE == pytest.approx(1500)
    assert model.rate("stamp_duty", "intraday", "buy") * CRORE == pytest.approx(300)
    assert model.rate("stamp_duty", "delivery", "sell") == 0.0


def test_intraday_round_trip(model: CostModel) -> None:
    buy = model.charges(Fill("buy", "intraday", 1000.0, 100, date(2026, 10, 5)))
    assert buy.brokerage == 20.0  # Rs 20 is below 0.1% of Rs 1 lakh
    assert buy.stt == 0.0 and buy.dp == 0.0
    assert buy.exchange_transaction == pytest.approx(100_000 * 306.99 / CRORE)
    assert buy.stamp_duty == pytest.approx(3.0)
    assert buy.sebi_fee == pytest.approx(0.1)
    gst_base = 20.0 + buy.exchange_transaction + buy.ipft  # SEBI fee and stamp duty are outside it
    assert buy.gst == pytest.approx(0.18 * gst_base)

    sell = model.charges(Fill("sell", "intraday", 1010.0, 100, date(2026, 10, 5)))
    assert sell.stt == 25.0  # 25.25 rounded to the rupee
    assert sell.stamp_duty == 0.0
    assert buy.total + sell.total == pytest.approx(82.68, abs=0.01)  # about 0.083% of the turnover


def test_small_intraday_orders_pay_the_capped_brokerage(model: CostModel) -> None:
    assert model.charges(Fill("buy", "intraday", 100.0, 10, date(2026, 10, 5))).brokerage == 1.0


def test_delivery_sell_pays_dp_once_per_scrip_day(model: CostModel) -> None:
    first = model.charges(Fill("sell", "delivery", 2000.0, 50, date(2026, 10, 5)))
    assert (first.stt, first.dp) == (100.0, 20.0)
    assert first.gst == pytest.approx(
        0.18 * (20.0 + first.exchange_transaction + 20.0 + first.ipft)
    )
    later = model.charges(Fill("sell", "delivery", 2000.0, 50, date(2026, 10, 5), dp_applies=False))
    assert later.dp == 0.0 and later.total < first.total
    bought = model.charges(Fill("buy", "delivery", 2000.0, 50, date(2026, 10, 5)))
    assert (bought.dp, bought.stamp_duty) == (0.0, pytest.approx(15.0))


def test_stt_rounds_half_up(model: CostModel) -> None:
    # 0.1% of Rs 2,500 is Rs 2.50: half-up gives 3 (Python's round() would give 2)
    assert model.charges(Fill("buy", "delivery", 250.0, 10, date(2026, 10, 5))).stt == 3.0


def test_unverified_dates_are_refused(model: CostModel) -> None:
    assert model.earliest_verified() == date(2026, 10, 3)  # broker pricing was read that day
    with pytest.raises(UnverifiedPeriod, match="brokerage"):
        model.charges(Fill("buy", "intraday", 100.0, 1, date(2025, 6, 2)), at=date(2025, 6, 2))
    with pytest.raises(UnverifiedPeriod, match="exchange_transaction"):
        model.rate("exchange_transaction", "delivery", "buy", date(2020, 12, 31))
    with pytest.raises(UnverifiedPeriod, match="sebi_fee"):
        model.rate("sebi_fee", "delivery", "buy", date(2020, 9, 1))  # the COVID-period proposal


def broken(**changes: Any) -> dict[str, Any]:
    spec: dict[str, Any] = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    for path, value in changes.items():
        node = spec["components"]
        *parents, leaf = path.split("__")
        for key in parents:
            node = node[int(key)] if key.isdigit() else node[key]
        node[leaf] = value
    return spec


@pytest.mark.parametrize(
    "spec",
    [
        broken(exchange_transaction__0__to="2023-05-01"),  # overlaps the next period
        broken(gst__0__base={"delivery": ["brokerage", "income_tax"], "intraday": []}),
        broken(stt__0__source=[]),
        broken(stt__0__delivery={"buy": 0.1, "sell": 0.001}),  # 10%: a typo, not a rate
        broken(stamp_duty__0__unexpected="x"),
    ],
)
def test_bad_rate_files_are_rejected(spec: dict[str, Any], tmp_path: Path) -> None:
    path = tmp_path / "costs.yaml"
    path.write_text(yaml.safe_dump(spec), encoding="utf-8")
    with pytest.raises(ValidationError):
        CostModel.load(path)


def test_broker_calculator_reply_is_read_in_model_terms() -> None:
    """Upstox's documented calculator reply (SYNTHETIC until the token lets a
    real one be saved): fields map onto the cost model's components."""
    from gats.sources.upstox import parse_charges

    documented = (
        b'{"status": "success", "data": {"charges": {"total": 208.27, "brokerage": 0.0, '
        b'"taxes": {"gst": 1.02, "stt": 175.0, "stamp_duty": 26.23}, "other_charges": '
        b'{"transaction": 5.68, "clearing": 0.0, "ipft": 0.17, "sebi_turnover": 0.17}, '
        b'"dp_plan": {"name": "DP3A", "min_expense": 18.5}}}}'
    )
    assert parse_charges(documented) == {
        "brokerage": 0.0,
        "stt": 175.0,
        "stamp_duty": 26.23,
        "gst": 1.02,
        "exchange_transaction": 5.68,
        "ipft": 0.17,
        "sebi_fee": 0.17,
        "clearing": 0.0,
        "total": 208.27,
    }
