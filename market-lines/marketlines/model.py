"""Game model: opponent-adjusted scoring ratings fitted by weighted ridge regression.

Each game gives two equations, one per team's score:

    points = league_avg + offense[team] - defense[opponent] +/- home_edge / 2

with home_edge applied only away from neutral sites. Recent games weigh more
(exponential half-life), and last season's games enter at a decayed weight, which
acts as the preseason prior. Ridge shrinkage pulls teams with few games toward
average. The fit gives a projected margin and total for any pairing; the spread
in real outcomes around those projections (sigma) turns them into cover, win and
over probabilities.

The same final scores also refit the margin tables that convert spread and total
moves into probability (`fit_mass`), replacing the hand estimates in sports.py.
"""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from .results import Result

DEFAULTS = {
    # half-life (days), ridge strength, starting sigma for margin and total
    "ncaaf": {"half_life": 120.0, "ridge": 4.0, "sd_margin": 15.5, "sd_total": 15.0},
    "ncaab": {"half_life": 60.0, "ridge": 4.0, "sd_margin": 11.0, "sd_total": 12.5},
    "ncaabase": {"half_life": 45.0, "ridge": 8.0, "sd_margin": 4.5, "sd_total": 4.5},
}


def norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


@dataclass
class Ratings:
    sport: str
    fitted_at: datetime
    league_avg: float
    home_edge: float
    offense: dict[str, float]
    defense: dict[str, float]
    games: dict[str, int]
    sd_margin: float
    sd_total: float

    def knows(self, team: str) -> bool:
        return team in self.offense

    def project(self, home: str, away: str, neutral: bool = False) -> tuple[float, float]:
        """(home margin, total) in points."""
        edge = 0.0 if neutral else self.home_edge / 2
        home_pts = self.league_avg + self.offense.get(home, 0) - self.defense.get(away, 0) + edge
        away_pts = self.league_avg + self.offense.get(away, 0) - self.defense.get(home, 0) - edge
        return home_pts - away_pts, home_pts + away_pts

    def power(self, team: str) -> float:
        """Points better than an average team on a neutral field."""
        return self.offense.get(team, 0) + self.defense.get(team, 0)


def fit(results: list[Result], sport: str, as_of: datetime, half_life: float | None = None,
        ridge: float | None = None) -> Ratings:
    """Fit ratings on games played before `as_of`."""
    cfg = DEFAULTS[sport]
    half_life = half_life or cfg["half_life"]
    ridge = cfg["ridge"] if ridge is None else ridge
    games = [r for r in results if r.played < as_of]
    teams = sorted({r.home for r in games} | {r.away for r in games})
    if not games:
        return Ratings(sport, as_of, 0.0, 0.0, {}, {}, {}, cfg["sd_margin"], cfg["sd_total"])
    index = {t: i for i, t in enumerate(teams)}
    n = len(teams)
    # Columns: intercept, home edge, offense x n, defense x n.
    rows, ys, ws = [], [], []
    for r in games:
        w = 0.5 ** ((as_of - r.played).total_seconds() / 86400 / half_life)
        h = 0.0 if r.neutral else 0.5
        for team, opp, pts, sign in ((r.home, r.away, r.home_pts, 1), (r.away, r.home, r.away_pts, -1)):
            rows.append((index[team], index[opp], sign * h))
            ys.append(pts)
            ws.append(w)
    m = len(rows)
    X = np.zeros((m, 2 + 2 * n))
    X[:, 0] = 1
    for i, (t, o, h) in enumerate(rows):
        X[i, 1] = h
        X[i, 2 + t] = 1
        X[i, 2 + n + o] = -1
    y, w = np.array(ys, float), np.array(ws, float)
    penalty = np.full(2 + 2 * n, ridge)
    penalty[:2] = 1e-6
    A = X.T @ (X * w[:, None]) + np.diag(penalty)
    beta = np.linalg.solve(A, X.T @ (w * y))

    counts: dict[str, int] = {}
    for r in games:
        counts[r.home] = counts.get(r.home, 0) + 1
        counts[r.away] = counts.get(r.away, 0) + 1
    return Ratings(
        sport=sport,
        fitted_at=as_of,
        league_avg=float(beta[0]),
        home_edge=float(beta[1]),
        offense={t: float(beta[2 + i]) for t, i in index.items()},
        defense={t: float(beta[2 + n + i]) for t, i in index.items()},
        games=counts,
        sd_margin=cfg["sd_margin"],
        sd_total=cfg["sd_total"],
    )


# Probabilities from a projection

def cover_prob(margin: float, home_line: float, sd: float) -> float:
    """Fair probability home covers `home_line` (pushes removed)."""
    edge = margin + home_line
    if float(home_line).is_integer():
        win = 1 - norm_cdf((0.5 - edge) / sd)
        push = norm_cdf((0.5 - edge) / sd) - norm_cdf((-0.5 - edge) / sd)
        return win / (1 - push) if push < 1 else 0.5
    return 1 - norm_cdf(-edge / sd)


def over_prob(total: float, line: float, sd: float) -> float:
    if float(line).is_integer():
        win = 1 - norm_cdf((line + 0.5 - total) / sd)
        push = norm_cdf((line + 0.5 - total) / sd) - norm_cdf((line - 0.5 - total) / sd)
        return win / (1 - push) if push < 1 else 0.5
    return 1 - norm_cdf((line - total) / sd)


def win_prob(margin: float, sd: float) -> float:
    return cover_prob(margin, 0.0, sd)


# Walk-forward evaluation

@dataclass
class Evaluation:
    games: int
    mae_margin: float
    rmse_margin: float
    mae_total: float
    rmse_total: float
    winner_right: float
    brier: float


def walk_forward(results: list[Result], sport: str, start: datetime, end: datetime, step_days: int = 7,
                 min_games: int = 3, **kwargs) -> tuple[Evaluation | None, list[tuple]]:
    """Refit every `step_days` on earlier games only and score the next window.

    Returns the summary and one row per scored game: (result, proj_margin, proj_total).
    Teams with fewer than `min_games` games in the fit are skipped.
    """
    scored = []
    at = start
    while at < end:
        ratings = fit(results, sport, at, **kwargs)
        window = [r for r in results if at <= r.played < at + timedelta(days=step_days)]
        for r in window:
            if ratings.games.get(r.home, 0) < min_games or ratings.games.get(r.away, 0) < min_games:
                continue
            margin, total = ratings.project(r.home, r.away, r.neutral)
            scored.append((r, margin, total))
        at += timedelta(days=step_days)
    if not scored:
        return None, []
    dm = np.array([r.margin - m for r, m, _ in scored], float)
    dt = np.array([r.total - t for r, _, t in scored], float)
    sd = float(np.sqrt(np.mean(dm**2)))
    probs = [(win_prob(m, sd), r.margin > 0) for r, m, _ in scored if r.margin != 0]
    return Evaluation(
        games=len(scored),
        mae_margin=float(np.mean(np.abs(dm))),
        rmse_margin=sd,
        mae_total=float(np.mean(np.abs(dt))),
        rmse_total=float(np.sqrt(np.mean(dt**2))),
        winner_right=float(np.mean([(p > 0.5) == won for p, won in probs])) if probs else 0.0,
        brier=float(np.mean([(p - won) ** 2 for p, won in probs])) if probs else 0.0,
    ), scored


# Refit the point-to-probability tables

def fit_mass(values: list[int], prior, strength: float = 200.0) -> dict[int, float]:
    """Smoothed P(value == k): observed frequencies shrunk toward the prior table.

    `values` are final margins (absolute) or totals. The prior keeps rarely seen
    numbers sensible; with a few thousand games the data dominates near the middle.
    """
    if not values:
        return {}
    counts: dict[int, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    n = len(values)
    lo, hi = min(values), max(values)
    return {k: (counts.get(k, 0) + strength * prior(k)) / (n + strength) for k in range(lo, hi + 1)}
