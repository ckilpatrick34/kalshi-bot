"""Sell overpriced longshots with resting maker orders.

Research on Kalshi (Whelan, "Makers and Takers") found cheap contracts win less often
than their price implies, and takers who buy them lose the most. This strategy rests
an order that sells the cheap side of a market:

* YES is cheap (price p in the band): rest an "ask" at p + edge, selling YES.
* NO is cheap (1 - p in the band): rest a "bid" at p - edge, buying YES, which is
  the same as selling the cheap NO.

Orders are re-priced as trades move the market and pulled when it leaves the band
or the market is outside the time window before close.
"""

from dataclasses import dataclass

from ..backtest import Context, Strategy


@dataclass
class LongshotMaker(Strategy):
    min_cheap: float = 0.02  # ignore contracts cheaper than this
    max_cheap: float = 0.10  # the "longshot" band for the cheap side
    edge: float = 0.0  # extra cents above the last trade we ask for
    contracts: float = 10
    max_position: float = 50
    min_hours_to_close: float = 0.0
    max_hours_to_close: float = 72.0
    sides: tuple = ("yes", "no")  # which cheap side(s) to sell
    # Sports markets close early when the game ends, so a settled market's close_time
    # leaks the result timing. Use "expected_expiration_time" there, which is known up front.
    clock: str = "close_time"

    def on_trade(self, ctx: Context, trade) -> None:
        ticker, p = trade.ticker, trade.price
        hours_left = (ctx.market(ticker)[self.clock] - ctx.now).total_seconds() / 3600
        in_window = self.min_hours_to_close <= hours_left <= self.max_hours_to_close
        pos = ctx.position(ticker)

        want = None
        if in_window and "yes" in self.sides and self.min_cheap <= p <= self.max_cheap and -pos < self.max_position:
            want = ("ask", round(min(0.99, p + self.edge), 2))
        elif in_window and "no" in self.sides and self.min_cheap <= 1 - p <= self.max_cheap and pos < self.max_position:
            want = ("bid", round(max(0.01, p - self.edge), 2))

        existing = ctx.orders(ticker)
        if any((o.side, o.price) == want for o in existing):
            return
        for o in existing:
            ctx.cancel(o)
        if want:
            room = self.max_position - abs(pos)
            ctx.place(ticker, want[0], want[1], min(self.contracts, room))
