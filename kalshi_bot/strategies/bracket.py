"""Find bracket ladders whose prices don't add up.

For an event whose markets are mutually exclusive AND exhaustive (exactly one settles
YES, like a daily-high ladder with "or below" and "or above" ends):

* Buying every YES at the ask costs sum(asks) + fees and pays $1.
* Buying every NO (selling every YES at the bid) costs sum(1 - bid) + fees and pays n - 1.

Either is a locked-in profit when the cost is below the payout. This needs live order
book quotes, so it runs as a scanner rather than inside the trade-replay backtester.
"""

from dataclasses import dataclass

from ..fees import taker_fee


@dataclass
class Quote:
    ticker: str
    yes_bid: float | None
    yes_ask: float | None
    bid_size: float = 0
    ask_size: float = 0
    fee_multiplier: float = 1.0


@dataclass
class BracketOpportunity:
    event_ticker: str
    direction: str  # "buy_all_yes" or "buy_all_no"
    contracts: float
    cost: float
    payout: float

    @property
    def profit(self) -> float:
        return round(self.payout - self.cost, 2)


def scan_event(event_ticker: str, quotes: list[Quote], max_contracts: float = 100) -> list[BracketOpportunity]:
    """Return locked-in arbitrages for one exhaustive, mutually exclusive event, after taker fees."""
    found = []
    n = len(quotes)
    if n < 2:
        return found

    if all(q.yes_ask is not None for q in quotes):
        size = min([max_contracts] + [q.ask_size for q in quotes])
        if size > 0:
            cost = sum(q.yes_ask * size + taker_fee(q.yes_ask, size, q.fee_multiplier) for q in quotes)
            if cost < size:
                found.append(BracketOpportunity(event_ticker, "buy_all_yes", size, round(cost, 2), size))

    if all(q.yes_bid is not None for q in quotes):
        size = min([max_contracts] + [q.bid_size for q in quotes])
        if size > 0:
            cost = sum((1 - q.yes_bid) * size + taker_fee(1 - q.yes_bid, size, q.fee_multiplier) for q in quotes)
            payout = (n - 1) * size
            if cost < payout:
                found.append(BracketOpportunity(event_ticker, "buy_all_no", size, round(cost, 2), payout))

    return found
