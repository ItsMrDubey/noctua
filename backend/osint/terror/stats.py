"""Small, dependency-free statistics for indicators (no raw ratios without a test).

* ``poisson_test(k, mu)``: exact one-sided tail probabilities for an observed count against
  an expected Poisson rate.
* ``nb_test(k, mean, var)``: the same against a negative binomial fitted by moments to the
  baseline window (over-dispersion: conflict counts are burstier than Poisson). Falls back
  to Poisson when the baseline is not over-dispersed.
* ``rate_ci(k, exposure)``: exact (Garwood) 90 % interval for a Poisson rate.
* ``chi2_shift(recent, baseline)``: chi-square test of a category mix against a baseline mix.
* ``gamma_sample``: posterior draws for a Poisson rate (uncertainty bands).
"""

from __future__ import annotations

import math
import random


def _log_pois(k: int, mu: float) -> float:
    return k * math.log(mu) - mu - math.lgamma(k + 1)


def poisson_cdf(k: int, mu: float) -> float:
    if k < 0:
        return 0.0
    if mu <= 0:
        return 1.0
    return min(1.0, sum(math.exp(_log_pois(i, mu)) for i in range(k + 1)))


def poisson_test(k: int, mu: float) -> dict:
    """{"p_high": P(X >= k), "p_low": P(X <= k)} for X ~ Poisson(mu)."""
    if mu <= 0:
        return {"p_high": 0.0 if k > 0 else 1.0, "p_low": 1.0, "model": "poisson"}
    p_low = poisson_cdf(k, mu)
    p_high = 1.0 - poisson_cdf(k - 1, mu)
    return {"p_high": max(0.0, min(1.0, p_high)), "p_low": max(0.0, min(1.0, p_low)),
            "model": "poisson"}


def _log_nb(k: int, r: float, p: float) -> float:
    return (math.lgamma(k + r) - math.lgamma(r) - math.lgamma(k + 1) + r * math.log(p)
            + k * math.log1p(-p))


def nb_test(k: int, mean: float, var: float) -> dict:
    """Negative binomial (method of moments: r = mean^2 / (var - mean)) tail probabilities.
    Poisson when var <= mean."""
    if mean <= 0:
        return poisson_test(k, mean)
    if var <= mean * 1.05:
        return poisson_test(k, mean)
    r = mean * mean / (var - mean)
    p = r / (r + mean)
    cdf_k = min(1.0, sum(math.exp(_log_nb(i, r, p)) for i in range(k + 1)))
    cdf_km1 = min(1.0, sum(math.exp(_log_nb(i, r, p)) for i in range(k))) if k > 0 else 0.0
    return {"p_high": max(0.0, min(1.0, 1.0 - cdf_km1)), "p_low": max(0.0, min(1.0, cdf_k)),
            "model": "negative_binomial", "r": round(r, 3)}


def _chi2_sf(x: float, dof: int) -> float:
    """Survival function of chi-square via the regularised upper incomplete gamma."""
    if x <= 0:
        return 1.0
    a, z = dof / 2.0, x / 2.0
    # series for P(a, z), continued fraction for Q(a, z)
    if z < a + 1:
        term = total = 1.0 / a
        n = a
        for _ in range(500):
            n += 1
            term *= z / n
            total += term
            if abs(term) < abs(total) * 1e-12:
                break
        p = total * math.exp(-z + a * math.log(z) - math.lgamma(a))
        return max(0.0, min(1.0, 1.0 - p))
    b = z + 1 - a
    c = 1e300
    d = 1 / b
    h = d
    for i in range(1, 500):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        d = 1e-300 if abs(d) < 1e-300 else d
        c = b + an / c
        c = 1e-300 if abs(c) < 1e-300 else c
        d = 1 / d
        delta = d * c
        h *= delta
        if abs(delta - 1) < 1e-12:
            break
    return max(0.0, min(1.0, math.exp(-z + a * math.log(z) - math.lgamma(a)) * h))


def chi2_shift(recent: dict[str, int], baseline: dict[str, int], *, min_expected: float = 1.0
               ) -> dict | None:
    """Has the category mix in ``recent`` shifted from ``baseline``? Expected counts are the
    baseline shares scaled to the recent total; sparse categories are pooled into "other".
    None when there is too little data for the test."""
    n_r, n_b = sum(recent.values()), sum(baseline.values())
    if n_r < 5 or n_b < 10:
        return None
    cats = set(recent) | set(baseline)
    exp = {c: n_r * baseline.get(c, 0) / n_b for c in cats}
    pooled_o = pooled_e = 0.0
    obs_e: list[tuple[float, float]] = []
    for c in cats:
        if exp[c] < min_expected:
            pooled_o += recent.get(c, 0)
            pooled_e += exp[c]
        else:
            obs_e.append((recent.get(c, 0), exp[c]))
    if pooled_e >= min_expected:
        obs_e.append((pooled_o, pooled_e))
    if len(obs_e) < 2:
        return None
    stat = sum((o - e) ** 2 / e for o, e in obs_e if e > 0)
    dof = len(obs_e) - 1
    return {"chi2": round(stat, 2), "dof": dof, "p": _chi2_sf(stat, dof)}


def rate_ci(k: int, exposure: float, level: float = 0.90) -> tuple[float, float]:
    """Exact Poisson interval for the rate k/exposure (bisection on the Poisson CDF)."""
    if exposure <= 0:
        return (0.0, 0.0)
    alpha = (1 - level) / 2

    def solve(target: float, kk: int) -> float:
        lo, hi = 0.0, max(10.0, kk * 4 + 10)
        for _ in range(80):
            mid = (lo + hi) / 2
            if poisson_cdf(kk, mid) > target:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2

    lower = 0.0 if k == 0 else solve(1 - alpha, k - 1)
    upper = solve(alpha, k)
    return (lower / exposure, upper / exposure)


def gamma_sample(k: int, exposure: float, n: int, rng: random.Random) -> list[float]:
    """Posterior draws of a Poisson rate under a Jeffreys prior: Gamma(k + 0.5, exposure)."""
    if exposure <= 0:
        return [0.0] * n
    return [rng.gammavariate(k + 0.5, 1.0 / exposure) for _ in range(n)]


def mean_var(xs: list[float]) -> tuple[float, float]:
    if not xs:
        return 0.0, 0.0
    m = sum(xs) / len(xs)
    v = sum((x - m) ** 2 for x in xs) / (len(xs) - 1) if len(xs) > 1 else 0.0
    return m, v


def describe_p(p_high: float, p_low: float, alpha: float = 0.05) -> str:
    if p_high < alpha:
        return "above baseline"
    if p_low < alpha:
        return "below baseline"
    return "within normal variation"
