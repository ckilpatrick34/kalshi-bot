"""Book lines, point-to-probability conversion, and market consensus.

Conventions used everywhere:

* Side A is the home team (h2h, spreads) or the over (totals); side B is away/under.
* `point` is the home team's spread (negative = home favoured) or the total.
  h2h lines have no point.
* A line's "equivalent probability" is side A's fair probability after moving the
  line to a reference point, so books on different numbers compare directly.
"""

import math
import statistics
from dataclasses import dataclass
from datetime import datetime

from .prices import devig_pair
from .sports import Sport

SIDES = {"h2h": ("home", "away"), "spreads": ("home", "away"), "totals": ("over", "under")}


@dataclass(frozen=True)
class Line:
    book: str
    point: float | None
    price_a: float
    price_b: float

    def fair_a(self) -> float:
        return devig_pair(self.price_a, self.price_b)

    def price(self, side: str) -> float:
        return self.price_a if side == "a" else self.price_b

    def side_point(self, market: str, side: str) -> float | None:
        """The number the bettor gets on that side (an away spread is the negative of home's)."""
        if self.point is None:
            return None
        return -self.point if market == "spreads" and side == "b" else self.point


def span_mass(lo: float, hi: float, mass) -> float:
    """Probability that an integer outcome falls between two lines; an outcome on a line counts half (push)."""
    if lo > hi:
        lo, hi = hi, lo
    if lo == hi:
        return 0.0
    total = 0.0
    for k in range(math.floor(lo), math.ceil(hi) + 1):
        if lo < k < hi:
            total += mass(k)
        elif k == lo or k == hi:
            total += mass(k) / 2
    return total


def shift_prob(sport: Sport, market: str, prob_a: float, from_point: float | None, to_point: float | None) -> float:
    """Side A's probability after the line moves from one point to another."""
    if market == "h2h" or from_point is None or to_point is None or from_point == to_point:
        return prob_a
    if market == "spreads":
        # Home covers when margin > -point; more points for home raises the cover chance.
        delta = span_mass(-to_point, -from_point, sport.margin_mass)
        prob = prob_a + delta if to_point > from_point else prob_a - delta
    else:
        # Over wins when total > point; a higher total lowers the over's chance.
        delta = span_mass(from_point, to_point, sport.total_mass)
        prob = prob_a - delta if to_point > from_point else prob_a + delta
    return min(max(prob, 0.001), 0.999)


def equivalent(sport: Sport, market: str, line: Line, ref_point: float | None) -> float:
    return shift_prob(sport, market, line.fair_a(), line.point, ref_point)


def ref_point(lines: list[Line]) -> float | None:
    """The market's common number: the median point, rounded to the half point."""
    points = [l.point for l in lines if l.point is not None]
    if not points:
        return None
    return round(statistics.median(points) * 2) / 2


@dataclass(frozen=True)
class Consensus:
    point: float | None
    fair_a: float  # side A's fair probability at `point`
    source: str    # the sharp book used, or "median"
    books: int


def consensus(sport: Sport, market: str, lines: list[Line], exclude: str | None = None) -> Consensus | None:
    """Fair value of the market: the first sharp book quoting it, else the median of all books."""
    pool = [l for l in lines if l.book != exclude]
    if not pool:
        return None
    point = ref_point(pool)
    for sharp in sport.sharp_books:
        line = next((l for l in pool if l.book == sharp), None)
        if line:
            return Consensus(point, equivalent(sport, market, line, point), sharp, len(pool))
    fair = statistics.median(equivalent(sport, market, l, point) for l in pool)
    return Consensus(point, fair, "median", len(pool))


def side_fair(sport: Sport, market: str, cons: Consensus, side: str, home_point: float | None) -> float:
    """Fair probability of one side at a given home spread / total."""
    p_a = shift_prob(sport, market, cons.fair_a, cons.point, home_point)
    return p_a if side == "a" else 1 - p_a


def best_line(market: str, lines: list[Line], side: str, sport: Sport, cons: Consensus) -> tuple[Line, float]:
    """The book offering the highest expected value on a side, and that value."""
    scored = [(l, side_fair(sport, market, cons, side, l.point) * l.price(side) - 1) for l in lines]
    return max(scored, key=lambda pair: pair[1])


class Timeline:
    """Each book's line over time for one event and market, from change-only rows.

    Rows are (ts, book, point, price_a, price_b); a row with no prices means the
    book pulled the market.
    """

    def __init__(self, rows: list[tuple]):
        self.rows = sorted(rows, key=lambda r: r[0])

    def at(self, when: datetime) -> list[Line]:
        state: dict[str, Line | None] = {}
        for ts, book, point, price_a, price_b in self.rows:
            if ts > when:
                break
            state[book] = Line(book, point, price_a, price_b) if price_a and price_b else None
        return [line for line in state.values() if line is not None]

    def first_time(self, min_books: int) -> datetime | None:
        """When the market first had enough books to call it open."""
        live: dict[str, bool] = {}
        for ts, book, _point, price_a, price_b in self.rows:
            live[book] = bool(price_a and price_b)
            if sum(live.values()) >= min_books:
                return ts
        return None
