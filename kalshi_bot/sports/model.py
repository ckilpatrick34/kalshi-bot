"""Elo game model: a win probability for every game from past results alone.

Each team has a rating. Before a game the home team's win probability is

    p_home = 1 / (1 + 10 ** (-(home_rating + home_advantage - away_rating) / 400))

After it, both ratings move by K x (result - p_home), scaled up for bigger wins
(less so when the favourite was already expected to win big). At the start of each
season ratings slide part of the way back to average, since rosters change.

Parameters follow FiveThirtyEight's published Elo models. `backtest` replays history
and scores the probabilities, so you can see how far to trust them before using them.
"""

import math
from dataclasses import dataclass, field
from datetime import timedelta

from .results import GameResult

MEAN = 1500.0
OFFSEASON = timedelta(days=60)  # a gap this long between a team's games starts a new season


@dataclass(frozen=True)
class EloParams:
    k: float
    home_advantage: float
    season_revert: float  # share of the distance to MEAN given back each new season
    margin: bool = True  # scale updates by margin of victory


PARAMS = {
    "nfl": EloParams(k=20, home_advantage=48, season_revert=1 / 3),
    "nba": EloParams(k=20, home_advantage=100, season_revert=1 / 4),
    "mlb": EloParams(k=4, home_advantage=24, season_revert=1 / 3, margin=False),
    "nhl": EloParams(k=6, home_advantage=50, season_revert=1 / 3),
    "ncaaf": EloParams(k=25, home_advantage=55, season_revert=1 / 3),
    "ncaab": EloParams(k=20, home_advantage=80, season_revert=1 / 3),
}


def win_probability(diff: float) -> float:
    return 1 / (1 + 10 ** (-diff / 400))


def margin_multiplier(margin: float, winner_diff: float) -> float:
    """FiveThirtyEight's NFL form: log of the margin, damped when the winner was the favourite."""
    return math.log(abs(margin) + 1) * 2.2 / (winner_diff * 0.001 + 2.2)


@dataclass
class Elo:
    params: EloParams
    ratings: dict[str, float] = field(default_factory=dict)
    last_played: dict = field(default_factory=dict)
    games_played: dict[str, int] = field(default_factory=dict)

    def rating(self, team: str, when=None) -> float:
        r = self.ratings.get(team, MEAN)
        last = self.last_played.get(team)
        if when is not None and last is not None and when - last > OFFSEASON:
            r = MEAN + (r - MEAN) * (1 - self.params.season_revert)
        return r

    def diff(self, home: str, away: str, neutral: bool = False, when=None) -> float:
        hfa = 0 if neutral else self.params.home_advantage
        return self.rating(home, when) + hfa - self.rating(away, when)

    def predict(self, home: str, away: str, neutral: bool = False, when=None) -> float:
        """Home team's win probability."""
        return win_probability(self.diff(home, away, neutral, when))

    def update(self, g: GameResult) -> float:
        """Learn from one finished game. Returns the pre-game home win probability."""
        when = g.start_time
        diff = self.diff(g.home, g.away, g.neutral, when)
        p = win_probability(diff)
        margin = g.home_score - g.away_score
        result = 1.0 if margin > 0 else 0.0 if margin < 0 else 0.5
        mult = 1.0
        if self.params.margin and margin != 0:
            mult = margin_multiplier(margin, diff if margin > 0 else -diff)
        shift = self.params.k * mult * (result - p)
        self.ratings[g.home] = self.rating(g.home, when) + shift
        self.ratings[g.away] = self.rating(g.away, when) - shift
        for t in (g.home, g.away):
            self.last_played[t] = when
            self.games_played[t] = self.games_played.get(t, 0) + 1
        return p

    def fit(self, games: list[GameResult]) -> "Elo":
        for g in sorted(games, key=lambda g: g.start):
            self.update(g)
        return self

    def experience(self, team: str) -> int:
        return self.games_played.get(team, 0)


def backtest(games: list[GameResult], params: EloParams, test_from) -> dict:
    """Walk forward through history; score predictions made for games on or after `test_from`.

    Every prediction uses only games before it. Lower log loss and Brier are better;
    the baseline always predicts the home team's historical win rate.
    """
    elo = Elo(params)
    rows = []
    for g in sorted(games, key=lambda g: g.start):
        p = elo.update(g)
        if g.start_time >= test_from and g.home_score != g.away_score:
            rows.append((p, 1.0 if g.home_score > g.away_score else 0.0))
    if not rows:
        return {"games": 0}
    home_rate = sum(y for _, y in rows) / len(rows)

    def log_loss(p, y):
        p = min(max(p, 1e-6), 1 - 1e-6)
        return -(y * math.log(p) + (1 - y) * math.log(1 - p))

    buckets = {}
    for p, y in rows:
        fav_p, fav_won = (p, y) if p >= 0.5 else (1 - p, 1 - y)
        b = min(int(fav_p * 10) / 10, 0.9)
        n, wins, total_p = buckets.get(b, (0, 0.0, 0.0))
        buckets[b] = (n + 1, wins + fav_won, total_p + fav_p)
    return {
        "games": len(rows),
        "accuracy": sum((p >= 0.5) == (y == 1) for p, y in rows) / len(rows),
        "log_loss": sum(log_loss(p, y) for p, y in rows) / len(rows),
        "baseline_log_loss": sum(log_loss(home_rate, y) for _, y in rows) / len(rows),
        "brier": sum((p - y) ** 2 for p, y in rows) / len(rows),
        # favourite's predicted vs actual win rate, by confidence band
        "calibration": {
            f"{b:.0%}-{b + 0.1:.0%}": {"games": n, "predicted": tp / n, "actual": w / n}
            for b, (n, w, tp) in sorted(buckets.items())
        },
    }
