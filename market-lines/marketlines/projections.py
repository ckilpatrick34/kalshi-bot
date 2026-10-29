"""Model upkeep: collect scores, refit ratings and tables, project upcoming games."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .model import DEFAULTS, Evaluation, Ratings, fit, fit_mass, walk_forward
from .sports import BASE, use_fitted_tables
from .store import Store
from .teams import TeamMatcher, normalise

# How far back the fit looks (two seasons, so last season is the preseason prior)
# and how much of the recent past the walk-forward check scores to set sigma.
HISTORY = timedelta(days=730)
CHECK_WINDOW = {"ncaaf": timedelta(days=75), "ncaab": timedelta(days=60), "ncaabase": timedelta(days=45)}


def collect(store: Store, espn, sport: str, start: date, end: date) -> tuple[int, int]:
    """Store final scores, and upcoming neutral sites, for each day in the range."""
    games = sites = 0
    for _day, results, neutral in espn.days(sport, start, end):
        store.insert_results(results)
        store.insert_neutral_sites(sport, neutral)
        store.commit()
        games += len(results)
        sites += len(neutral)
    return games, sites


@dataclass
class Refit:
    ratings: Ratings
    check: Evaluation | None
    projected: int
    unmatched: set[str]
    tables: dict[str, int]


def refit(store: Store, sport: str, now: datetime) -> Refit:
    """Refit ratings and margin/total tables, then project every upcoming event in the odds feed."""
    results = store.results(sport, since=now - HISTORY)

    check, _ = walk_forward(results, sport, now - CHECK_WINDOW[sport], now)
    ratings = fit(results, sport, now)
    if check and check.games >= 100:
        ratings.sd_margin, ratings.sd_total = check.rmse_margin, check.rmse_total

    tables = {}
    if len(results) >= 300:
        base = BASE[sport]
        margins = fit_mass([abs(r.margin) for r in results], lambda k: base.margin_mass(k) * (2 if k else 1))
        store.save_mass_table(sport, "margin", {k: p / 2 if k else p for k, p in margins.items()})
        store.save_mass_table(sport, "total", fit_mass([r.total for r in results], base.total_mass))
        tables = {"margin": len(margins), "total": len(results)}
        use_fitted_tables(store.mass_tables())

    matcher = TeamMatcher(list(ratings.offense))
    neutral = {(normalise(h), normalise(a)) for h, a, _ in store.neutral_sites(sport, now - timedelta(days=1))}
    rows = []
    for event_id, _sport, commence, home, away in store.events(sport):
        if commence <= now:
            continue
        h, a = matcher.match(home), matcher.match(away)
        if h is None or a is None:
            continue
        on_neutral = (normalise(home), normalise(away)) in neutral or (normalise(h), normalise(a)) in neutral
        margin, total = ratings.project(h, a, on_neutral)
        rows.append((event_id, sport, margin, total, ratings.sd_margin, ratings.sd_total,
                     ratings.power(h), ratings.power(a), now))
    store.save_projections(rows)
    store.commit()
    return Refit(ratings, check, len(rows), matcher.unmatched, tables)

