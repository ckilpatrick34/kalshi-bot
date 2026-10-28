"""Turn a sharp book's prices into fair, margin-free probabilities.

A book's implied probabilities (1 / decimal odds) add up to more than 1; the excess
is its margin. Two ways to remove it:

* multiplicative: scale every probability down by the same factor.
* power: find k with sum(p_i ** k) == 1. This takes more margin off longshots than
  favourites, which matches how books actually shade prices, so it's the default.
"""


def american_to_decimal(american: float) -> float:
    if abs(american) < 100:
        raise ValueError(f"Not American odds: {american}")
    return 1 + (american / 100 if american > 0 else 100 / -american)


def implied(decimal_odds: list[float]) -> list[float]:
    if any(d <= 1 for d in decimal_odds):
        raise ValueError(f"Decimal odds must be above 1: {decimal_odds}")
    return [1 / d for d in decimal_odds]


def multiplicative(decimal_odds: list[float]) -> list[float]:
    probs = implied(decimal_odds)
    total = sum(probs)
    return [p / total for p in probs]


def power(decimal_odds: list[float], tol: float = 1e-12) -> list[float]:
    probs = implied(decimal_odds)
    if sum(probs) <= 1:
        return multiplicative(decimal_odds)  # no margin to remove (or a stale/arb line)
    lo, hi = 1.0, 10.0
    for _ in range(200):
        k = (lo + hi) / 2
        if sum(p**k for p in probs) > 1:
            lo = k
        else:
            hi = k
        if hi - lo < tol:
            break
    k = (lo + hi) / 2
    fair = [p**k for p in probs]
    total = sum(fair)
    return [p / total for p in fair]


METHODS = {"multiplicative": multiplicative, "power": power}


def devig(decimal_odds: list[float], method: str = "power") -> list[float]:
    return METHODS[method](decimal_odds)
