"""Statistics and the M3 report (T3.4)."""

from __future__ import annotations

import math
import random
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from typer.testing import CliRunner

from gats.research.event_study import write_parquet
from gats.research.report import LIQUIDITY_BUCKETS, build_report, liquidity_table, spot_check_events
from gats.research.stats import (
    bh_adjust,
    cluster_bootstrap_means,
    clustered_mean,
    fcr_lower_bound,
    regularized_beta,
    t_sf,
)
from gats.research.study import load_study
from tests.test_study_guards import CONFIG, PREREG


class TestDistributions:
    @pytest.mark.parametrize(
        ("t", "df", "p"),
        [
            (2.228, 10, 0.025),  # textbook critical values
            (1.812, 10, 0.05),
            (1.697, 30, 0.05),
            (2.042, 30, 0.025),
            (1.96, math.inf, 0.025),
            (0.0, 7, 0.5),
            (-1.812, 10, 0.95),
        ],
    )
    def test_t_sf_matches_tables(self, t: float, df: float, p: float) -> None:
        assert t_sf(t, df) == pytest.approx(p, abs=2e-4)

    def test_cauchy_is_exact(self) -> None:
        for t in (0.3, 1.0, 2.5, 10.0):
            assert t_sf(t, 1) == pytest.approx(0.5 - math.atan(t) / math.pi, abs=1e-10)

    def test_incomplete_beta_identities(self) -> None:
        for x in (0.1, 0.5, 0.9):
            assert regularized_beta(1, 1, x) == pytest.approx(x)
            assert regularized_beta(3, 1, x) == pytest.approx(x**3)
            assert regularized_beta(2, 5, x) + regularized_beta(5, 2, 1 - x) == pytest.approx(1)


class TestClusteredMean:
    def test_hand_computed(self) -> None:
        # Mean 2.5; residual sums per cluster -2 and +2; CR1 SE = sqrt(2 * 8) / 4 = 1.
        r = clustered_mean([1, 2, 3, 4], ["a", "a", "b", "b"])
        assert (r.n, r.clusters, r.mean, r.median, r.hit_rate) == (4, 2, 2.5, 2.5, 1.0)
        assert r.se == pytest.approx(1.0) and r.t == pytest.approx(2.5)
        assert r.p_one_sided == pytest.approx(0.5 - math.atan(2.5) / math.pi)

    def test_clustering_widens_correlated_errors(self) -> None:
        rng = random.Random(1)
        days = [d for d in range(50) for _ in range(10)]
        shocks = {d: rng.gauss(0, 1) for d in range(50)}
        values = [shocks[d] + rng.gauss(0, 0.1) for d in days]
        naive = clustered_mean(values, list(range(len(values))))
        clustered = clustered_mean(values, days)
        assert clustered.se > 2 * naive.se

    def test_empty(self) -> None:
        assert clustered_mean([], []).n == 0


def test_bh_hand_computed() -> None:
    assert bh_adjust([0.01, 0.04, 0.03, 0.005]) == pytest.approx([0.02, 0.04, 0.04, 0.02])
    adjusted = bh_adjust([0.2, math.nan, 0.01])
    assert math.isnan(adjusted[1]) and adjusted[2] == pytest.approx(0.02)


def test_bootstrap_is_deterministic_and_centred() -> None:
    rng = random.Random(2)
    values = [rng.gauss(0.01, 0.02) for _ in range(400)]
    clusters = [i // 4 for i in range(400)]
    a = cluster_bootstrap_means(values, clusters, 2000, seed=9)
    b = cluster_bootstrap_means(values, clusters, 2000, seed=9)
    assert np.array_equal(a, b)
    assert float(a.mean()) == pytest.approx(sum(values) / 400, abs=5e-4)
    # FCR level: 1 selected of 20 at q=0.05 -> 0.25% quantile, below the 2.5% one.
    assert fcr_lower_bound(a, 1, 20, 0.05) <= float(np.quantile(a, 0.025))


def synthetic_rows(
    effect: dict[str, float], n_days: int = 150, per_day: int = 2
) -> list[dict[str, Any]]:
    cfg, _ = load_study(CONFIG)
    rng = random.Random(4)
    rows: list[dict[str, Any]] = []
    start = date(2024, 1, 2)
    ann = 0
    for event_type in cfg.event_types:
        for k in range(n_days):
            day = start + timedelta(days=k)
            for _ in range(per_day):
                ann += 1
                row: dict[str, Any] = {
                    "announcement_id": ann,
                    "event_type": event_type,
                    "confirmatory": event_type in cfg.events.confirmatory,
                    "entry_date": day,
                    "period": "test",
                    "filter_reason": None,
                    "symbol": f"S{ann % 50}",
                    "entry_open": 100.0,
                    "median_turnover_rs": rng.choice([2e7, 2e8, 2e9]),
                }
                for x in cfg.exits:
                    abnormal = effect.get(event_type, 0.0) + rng.gauss(0, 0.02)
                    row[f"ret_{x.name}"] = abnormal + 0.001
                    row[f"bench_{x.name}"] = 0.001
                    row[f"abnormal_{x.name}"] = abnormal
                    row[f"net_{x.name}"] = abnormal - cfg.costs.round_trip
                rows.append(row)
    # A few train-period and filtered rows, which must not affect the G1 cells.
    rows.append({**rows[0], "announcement_id": 10**6, "period": "train"})
    rows.append({**rows[0], "announcement_id": 10**6 + 1, "filter_reason": "illiquid"})
    return rows


def test_report_passes_a_real_effect_and_only_that() -> None:
    cfg, digest = load_study(CONFIG)
    text, family = build_report(
        synthetic_rows({"ORDER_WIN": 0.02}),
        cfg,
        config_hash=digest,
        run_id="r1",
        fingerprint={},
        figure=None,
    )
    passing = {(c.event_type, c.exit) for c in family if c.passes}
    assert passing == {("ORDER_WIN", x.name) for x in cfg.exits}
    assert "**PASS** for: ORDER_WIN d0" in text
    assert len(family) == 20
    # Zero true effect: the 0.50% cost makes every mean negative.
    text, family = build_report(
        synthetic_rows({}), cfg, config_hash=digest, run_id="r2", fingerprint={}, figure=None
    )
    assert not any(c.passes for c in family)
    assert "no edge found at the daily horizon" in text


def test_small_samples_cannot_pass() -> None:
    cfg, digest = load_study(CONFIG)
    rows = synthetic_rows({"ORDER_WIN": 0.05}, n_days=20)  # 40 events per type
    _, family = build_report(rows, cfg, config_hash=digest, run_id="r", fingerprint={}, figure=None)
    order = [c for c in family if c.event_type == "ORDER_WIN"]
    assert all(not c.passes and any("N_test 40 < 100" in r for r in c.reasons) for c in order)


def test_liquidity_and_spot_checks() -> None:
    cfg, _ = load_study(CONFIG)
    rows = synthetic_rows({})
    table = liquidity_table(rows, cfg)
    assert len(table) == len(cfg.events.confirmatory) * len(LIQUIDITY_BUCKETS) * len(cfg.exits)
    picks = spot_check_events(rows, cfg)
    assert len(picks) == 3 and all(p["confirmatory"] and p["period"] == "test" for p in picks)


def test_report_command_rebuilds_from_a_saved_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gats.cli import app

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GATS_DATA_DIR", str(tmp_path / "data"))
    cfg, _ = load_study(CONFIG)
    write_parquet(
        synthetic_rows({"BUYBACK": 0.02}),
        tmp_path / "data" / "research" / cfg.study / "r9" / "events.parquet",
    )
    result = CliRunner().invoke(
        app, ["research", "report", "--run", "r9", "--config", str(CONFIG), "--prereg", str(PREREG)]
    )
    assert result.exit_code == 0, result.stdout
    report = (tmp_path / "reports" / "M3_event_study.md").read_text(encoding="utf-8")
    assert "BUYBACK d1" in report and (tmp_path / "reports" / "figures" / "m3_car.png").exists()
