"""Price conversions and margin removal.

A book's two implied probabilities (1 / decimal odds) add up to more than 1; the
excess is its margin. The power method finds k with p_a**k + p_b**k == 1, which
takes more margin off the longshot than the favourite, as books actually shade.
"""


def american_to_decimal(american: float) -> float:
    if abs(american) < 100:
        raise ValueError(f"Not American odds: {american}")
    return 1 + (american / 100 if american > 0 else 100 / -american)


def decimal_to_american(decimal_odds: float) -> int:
    if decimal_odds <= 1:
        raise ValueError(f"Decimal odds must be above 1: {decimal_odds}")
    if decimal_odds >= 2:
        return round((decimal_odds - 1) * 100)
    return round(-100 / (decimal_odds - 1))


def devig_pair(price_a: float, price_b: float, tol: float = 1e-12) -> float:
    """Fair probability of side A from a two-way pair of decimal prices."""
    if price_a <= 1 or price_b <= 1:
        raise ValueError(f"Decimal odds must be above 1: {price_a}, {price_b}")
    pa, pb = 1 / price_a, 1 / price_b
    if pa + pb <= 1:
        return pa / (pa + pb)  # no margin to remove (a stale or arb pair)
    lo, hi = 1.0, 10.0
    while hi - lo > tol:
        k = (lo + hi) / 2
        if pa**k + pb**k > 1:
            lo = k
        else:
            hi = k
    k = (lo + hi) / 2
    fa, fb = pa**k, pb**k
    return fa / (fa + fb)
