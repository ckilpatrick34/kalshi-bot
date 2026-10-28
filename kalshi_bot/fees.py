"""Kalshi trading fees.

taker fee = ceil_to_cent(0.07   * M * C * P * (1 - P))
maker fee = ceil_to_cent(0.0175 * M * C * P * (1 - P))

P is the price in dollars, C the contract count, M the series multiplier.
On standard series (fee_type "quadratic") makers pay nothing. Series with
fee_type "quadratic_with_maker_fees" charge makers with M=1, and combo series
("quadratic_with_combo_maker_fees") with M=2. No fee is charged at settlement.

Polymarket US sports contracts charge takers ceil_to_cent(0.05 * C * P * (1 - P))
and makers nothing (Polymarket US fee schedule, Oct 2026). Rounding to the cent
isn't documented there, so it is rounded up here to stay conservative.
"""

import math

TAKER_RATE = 0.07
MAKER_RATE = 0.0175
POLYMARKET_US_TAKER_RATE = 0.05

MAKER_MULTIPLIER_BY_FEE_TYPE = {
    "quadratic": 0.0,
    "quadratic_with_maker_fees": 1.0,
    "quadratic_with_combo_maker_fees": 2.0,
}


def _ceil_to_cent(dollars: float) -> float:
    # Small epsilon so float noise like 1.7500000001 doesn't round up a cent.
    return math.ceil(dollars * 100 - 1e-9) / 100


def taker_fee(price: float, count: float, multiplier: float = 1.0) -> float:
    return _ceil_to_cent(TAKER_RATE * multiplier * count * price * (1 - price))


def maker_fee(price: float, count: float, fee_type: str = "quadratic") -> float:
    multiplier = MAKER_MULTIPLIER_BY_FEE_TYPE.get(fee_type, 1.0)
    if multiplier == 0:
        return 0.0
    return _ceil_to_cent(MAKER_RATE * multiplier * count * price * (1 - price))


def polymarket_us_taker_fee(price: float, count: float) -> float:
    return _ceil_to_cent(POLYMARKET_US_TAKER_RATE * count * price * (1 - price))
