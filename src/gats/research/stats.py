"""Statistics for event studies: clustered tests, cluster bootstrap, FDR.

Events on the same day are not independent (a market-wide move hits all of
them), so standard errors are clustered by entry date and the bootstrap
resamples whole dates. Testing 20 hypotheses at once guarantees some false
positives at 5%, so p-values are Benjamini-Hochberg adjusted and the
confidence bounds of the selected tests are false-coverage-rate adjusted
(Benjamini-Yekutieli 2005).

The Student-t CDF comes from :mod:`gats.tdist` (plain Python, no scipy).
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Hashable, Sequence
from dataclasses import dataclass

import numpy as np

from gats.tdist import t_sf


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
