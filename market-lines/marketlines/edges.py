"""Test the model against the betting market, and price an upcoming slate.

A scores-only model knows less than the market (injuries, weather, motivation), so
its raw number should not be bet straight. The backtest measures how much of the
gap between model and market actually showed up in results:

    actual margin ~ market margin + w * (model margin - market margin)

w = 0 means the model adds nothing beyond the line; w = 1 means trust it fully.
The slate uses that blended number, with the blended error's spread as sigma, so
an edge only appears where history says the model's disagreement paid.
"""

from dataclasses import dataclass
from datetime import datetime

import numpy as np

from .model import Ratings, cover_prob, over_prob, walk_forward, win_prob
from .results import BookLine, Result

STANDARD = 1.9090909  # -110


@dataclass
class Bucket:
    label: str
    bets: int
    wins: int
    pushes: int
    profit: float

    @property
    def win_rate(self) -> float:
        decided = self.bets - self.pushes
        return self.wins / decided if decided else 0.0

    @property
    def roi(self) -> float:
        return self.profit / self.bets if self.bets else 0.0


@dataclass
class MarketTest:
    games: int
    model_mae: float
    market_mae: float
    weight: float          # blend weight on the model's disagreement (margin)
    sd: float              # error of the blended margin
    total_games: int
    total_model_mae: float
    total_market_mae: float
    total_weight: float
    total_sd: float
    spread_buckets: list[Bucket]
    total_buckets: list[Bucket]


EDGE_BUCKETS = [(0, 2), (2, 4), (4, 7), (7, 1000)]


def _grade(rows, buckets=EDGE_BUCKETS) -> list[Bucket]:
    """rows: (edge in points, result vs line in points, price taken)."""
    out = []
    for lo, hi in buckets:
        picked = [(e, r, p) for e, r, p in rows if lo <= abs(e) < hi]
        wins = pushes = 0
        profit = 0.0
        for edge, result, price in picked:
            outcome = result if edge > 0 else -result
            if outcome == 0:
                pushes += 1
            elif outcome > 0:
                wins += 1
                profit += price - 1
            else:
                profit -= 1
        label = f"{lo}+ pts" if hi >= 1000 else f"{lo}-{hi} pts"
        out.append(Bucket(label, len(picked), wins, pushes, profit))
    return out


def _blend_weight(actual, market, model) -> float:
    gap = model - market
    if not np.any(gap):
        return 0.0
    w = float(np.dot(gap, actual - market) / np.dot(gap, gap))
    return min(max(w, 0.0), 1.0)


def market_test(results: list[Result], lines: list[BookLine], sport: str, start: datetime, end: datetime,
                **kwargs) -> tuple[MarketTest | None, list[tuple]]:
    """Walk-forward model vs the book's closing lines on the same games."""
    by_id = {l.game_id: l for l in lines}
    _, scored = walk_forward(results, sport, start, end, **kwargs)
    rows = [(r, m, t, by_id[r.game_id]) for r, m, t in scored if r.game_id in by_id]
    spread_rows = [(r, m, l) for r, m, _t, l in rows if l.spread is not None]
    total_rows = [(r, t, l) for r, _m, t, l in rows if l.total is not None]
    if len(spread_rows) < 30:
        return None, rows

    actual = np.array([r.margin for r, _, _ in spread_rows], float)
    market = np.array([-l.spread for _, _, l in spread_rows], float)
    model = np.array([m for _, m, _ in spread_rows], float)
    w = _blend_weight(actual, market, model)
    blended = market + w * (model - market)

    t_actual = np.array([r.total for r, _, _ in total_rows], float)
    t_market = np.array([l.total for _, _, l in total_rows], float)
    t_model = np.array([t for _, t, _ in total_rows], float)
    tw = _blend_weight(t_actual, t_market, t_model) if total_rows else 0.0
    t_blended = t_market + tw * (t_model - t_market)

    spread_grades = _grade([
        (m + l.spread, r.margin + l.spread,
         (l.spread_home_price if m + l.spread > 0 else l.spread_away_price) or STANDARD)
        for r, m, l in spread_rows
    ])
    total_grades = _grade([
        (t - l.total, r.total - l.total, (l.over_price if t > l.total else l.under_price) or STANDARD)
        for r, t, l in total_rows
    ])
    return MarketTest(
        games=len(spread_rows),
        model_mae=float(np.mean(np.abs(actual - model))),
        market_mae=float(np.mean(np.abs(actual - market))),
        weight=w,
        sd=float(np.sqrt(np.mean((actual - blended) ** 2))),
        total_games=len(total_rows),
        total_model_mae=float(np.mean(np.abs(t_actual - t_model))) if total_rows else 0.0,
        total_market_mae=float(np.mean(np.abs(t_actual - t_market))) if total_rows else 0.0,
        total_weight=tw,
        total_sd=float(np.sqrt(np.mean((t_actual - t_blended) ** 2))) if total_rows else 0.0,
        spread_buckets=spread_grades,
        total_buckets=total_grades,
    ), rows


@dataclass
class Play:
    line: BookLine
    market: str        # spread, total, moneyline
    side: str          # team name, Over or Under
    number: float | None
    price: float
    prob: float
    ev: float
    model_number: float
    market_number: float


def price_slate(ratings: Ratings, test: MarketTest, lines: list[BookLine], min_games: int = 4) -> list[Play]:
    """Every side of every upcoming line, valued with the blended model."""
    plays = []
    sd, total_sd = max(test.sd, 1.0), max(test.total_sd, 1.0)
    for l in lines:
        if ratings.games.get(l.home, 0) < min_games or ratings.games.get(l.away, 0) < min_games:
            continue
        model_margin, model_total = ratings.project(l.home, l.away, l.neutral)
        if l.spread is not None:
            market = -l.spread
            margin = market + test.weight * (model_margin - market)
            p_home = cover_prob(margin, l.spread, sd)
            for side, number, price, prob in ((l.home, l.spread, l.spread_home_price, p_home),
                                              (l.away, -l.spread, l.spread_away_price, 1 - p_home)):
                price = price or STANDARD
                plays.append(Play(l, "spread", side, number, price, prob, prob * price - 1,
                                  -model_margin if side == l.home else model_margin, number))
            if l.ml_home and l.ml_away:
                p_win = win_prob(margin, sd)
                for side, price, prob in ((l.home, l.ml_home, p_win), (l.away, l.ml_away, 1 - p_win)):
                    plays.append(Play(l, "moneyline", side, None, price, prob, prob * price - 1,
                                      model_margin if side == l.home else -model_margin, market))
        if l.total is not None:
            total = l.total + test.total_weight * (model_total - l.total)
            p_over = over_prob(total, l.total, total_sd)
            for side, price, prob in (("Over", l.over_price, p_over), ("Under", l.under_price, 1 - p_over)):
                price = price or STANDARD
                plays.append(Play(l, "total", side, l.total, price, prob, prob * price - 1, model_total, l.total))
    return sorted(plays, key=lambda p: p.ev, reverse=True)

