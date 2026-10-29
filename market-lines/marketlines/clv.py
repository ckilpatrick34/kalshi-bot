"""Closing lines and closing-line value per signal.

The close is each book's last quote before the scheduled start, valued like any
other consensus (first sharp book quoting, else the median). Three numbers per signal:

* clv_ev: the side's fair probability at the close, at the number the signal named,
  times the signal's price, minus 1. Positive means the alert's bet beat the close.
* clv_move: how far the side's fair probability moved from signal time to the close
  at the signal-time consensus number. It scores the prediction alone, without
  credit for shopping books.
* clv_points: points gained on the number (spreads and totals only).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from .lines import Consensus, Timeline, consensus, side_fair
from .sports import Sport


@dataclass(frozen=True)
class SignalClv:
    clv_ev: float
    clv_move: float
    clv_points: float | None


def closing(sport: Sport, market: str, tl: Timeline, commence: datetime) -> Consensus | None:
    lines = tl.at(commence - timedelta(microseconds=1))
    if len(lines) < sport.min_books:
        return None
    return consensus(sport, market, lines)


def signal_clv(sport: Sport, market: str, side: str, line: float | None, price: float,
               cons_point: float | None, cons_fair_a: float, close: Consensus) -> SignalClv:
    fair_close = side_fair(sport, market, close, side, line)
    then = cons_fair_a if side == "a" else 1 - cons_fair_a
    move = side_fair(sport, market, close, side, cons_point) - then
    points = None
    if market != "h2h" and line is not None and close.point is not None:
        gained = line - close.point
        # Home wants more points on the spread; the over wants a lower total.
        points = gained if (market == "spreads") == (side == "a") else -gained
    return SignalClv(clv_ev=fair_close * price - 1, clv_move=move, clv_points=points)
