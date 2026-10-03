"""Statistics for event studies: clustered tests, cluster bootstrap, FDR.

Events on the same day are not independent (a market-wide move hits all of
them), so standard errors are clustered by entry date and the bootstrap
resamples whole dates. Testing 20 hypotheses at once guarantees some false
positives at 5%, so p-values are Benjamini-Hochberg adjusted and the
confidence bounds of the selected tests are false-coverage-rate adjusted
(Benjamini-Yekutieli 2005).

The Student-t CDF is computed from the regularized incomplete beta function
(continued fraction, as in Numerical Recipes) to avoid a scipy dependency
for one function; tests pin it to textbook critical values.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Hashable, Sequence
from dataclasses import dataclass

import numpy as np


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function (modified Lentz)."""
    tiny, eps = 1e-300, 1e-15
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            return h
    return h


def regularized_beta(a: float, b: float, x: float) -> float:
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    log_front = (
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    )
    front = math.exp(log_front)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def t_sf(t: float, df: float) -> float:
    """P(T > t) for Student's t with ``df`` degrees of freedom."""
    if math.isinf(df):
        return 0.5 * math.erfc(t / math.sqrt(2.0))
    tail = 0.5 * regularized_beta(df / 2.0, 0.5, df / (df + t * t))
    return tail if t >= 0 else 1.0 - tail


@dataclass(frozen=True)
class ClusteredMean:
    n: int
    clusters: int
    mean: float
    median: float
    hit_rate: float  # share of observations > 0
    se: float
    t: float
    p_one_sided: float  # H1: mean > 0


def clustered_mean(values: Sequence[float], clusters: Sequence[Hashable]) -> ClusteredMean:
    """Mean with a cluster-robust (CR1) standard error and a one-sided t-test,
    df = clusters - 1."""
    x = np.asarray(values, dtype=float)
    n = len(x)
    if n == 0:
        return ClusteredMean(0, 0, math.nan, math.nan, math.nan, math.nan, math.nan, math.nan)
    mean = float(x.mean())
    sums: dict[Hashable, float] = defaultdict(float)
    for value, cluster in zip(x - mean, clusters, strict=True):
        sums[cluster] += float(value)
    g = len(sums)
    squares = sum(s * s for s in sums.values())
    se = math.sqrt(g / (g - 1) * squares) / n if g >= 2 else math.nan
    t = mean / se if se and se > 0 else math.nan
    p = t_sf(t, g - 1) if not math.isnan(t) else math.nan
    return ClusteredMean(
        n=n,
        clusters=g,
        mean=mean,
        median=float(np.median(x)),
        hit_rate=float((x > 0).mean()),
        se=se,
        t=t,
        p_one_sided=p,
    )


def cluster_bootstrap_means(
    values: Sequence[float], clusters: Sequence[Hashable], resamples: int, seed: int
) -> np.ndarray:
    """Means of ``resamples`` cluster-bootstrap samples (dates drawn with replacement)."""
    groups: dict[Hashable, list[float]] = defaultdict(list)
    for value, cluster in zip(values, clusters, strict=True):
        groups[cluster].append(float(value))
    sums = np.array([sum(v) for v in groups.values()])
    counts = np.array([len(v) for v in groups.values()])
    rng = np.random.default_rng(seed)
    picks = rng.integers(0, len(sums), size=(resamples, len(sums)))
    result: np.ndarray = sums[picks].sum(axis=1) / counts[picks].sum(axis=1)
    return result


def bh_adjust(p_values: Sequence[float]) -> list[float]:
    """Benjamini-Hochberg adjusted p-values (NaN stays NaN and is not counted)."""
    indexed = [(p, i) for i, p in enumerate(p_values) if not math.isnan(p)]
    m = len(indexed)
    adjusted = [math.nan] * len(p_values)
    running = 1.0
    for rank, (p, i) in reversed(list(enumerate(sorted(indexed), start=1))):
        running = min(running, p * m / rank)
        adjusted[i] = running
    return adjusted


def fcr_lower_bound(boot_means: np.ndarray, selected: int, family: int, q: float) -> float:
    """One-sided lower bound at level 1 - R*q/m for a BH-selected test
    (Benjamini-Yekutieli false coverage rate)."""
    level = max(selected, 1) * q / family
    return float(np.quantile(boot_means, level))
