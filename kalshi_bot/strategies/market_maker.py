"""Two-sided market making: earn the spread by resting a bid below and an ask above the last trade.

Quotes are re-centred on each trade and skewed against inventory, so a position that
builds up on one side gets cheaper to unwind. No order book history exists, so fills are
inferred from later trades exactly as in the longshot strategy, which flatters fills at
our own price; check results with queue_fill_fraction=0 before trusting them.
"""

from dataclasses import dataclass

from ..backtest import Context, Strategy


@dataclass
class MarketMaker(Strategy):
    half_spread: float = 0.02  # quote this far either side of the last trade
    skew_per_contract: float = 0.001  # shift both quotes down this much per contract held long
    contracts: float = 10
    max_position: float = 50
    min_price: float = 0.10  # stay out of longshots, where one-sided flow dominates
    max_price: float = 0.90
    min_hours_to_close: float = 1.0  # stop before the final hour, when news moves prices fast
    max_hours_to_close: float = 72.0
    clock: str = "close_time"

    def on_trade(self, ctx: Context, trade) -> None:
        ticker, p = trade.ticker, trade.price
        hours_left = (ctx.market(ticker)[self.clock] - ctx.now).total_seconds() / 3600
        pos = ctx.position(ticker)
        quoting = self.min_hours_to_close <= hours_left <= self.max_hours_to_close and self.min_price <= p <= self.max_price

        want = set()
        if quoting:
            mid = p - self.skew_per_contract * pos
            bid, ask = round(mid - self.half_spread, 2), round(mid + self.half_spread, 2)
            if pos < self.max_position and 0 < bid < 1:
                want.add(("bid", bid))
            if -pos < self.max_position and 0 < ask < 1:
                want.add(("ask", ask))

        for o in ctx.orders(ticker):
            if (o.side, o.price) in want:
                want.discard((o.side, o.price))
            else:
                ctx.cancel(o)
        for side, price in want:
            room = self.max_position - (pos if side == "bid" else -pos)
            ctx.place(ticker, side, price, min(self.contracts, room))
