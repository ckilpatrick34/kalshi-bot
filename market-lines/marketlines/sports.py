"""Per-sport settings: feed keys, key numbers, how much a point is worth, signal thresholds.

Movement thresholds are in probability units (0.02 = two percentage points of
side-A win/cover probability) so one detector works for moneylines, spreads and
totals. A spread or total line is converted to a probability at a common point
with the margin tables below.

The margin tables are starting estimates, not fitted values. P(final margin == k)
for college football puts extra mass on 3, 7, 10, 14 and 17; college basketball
margins are close to flat near zero. Refit both from final scores once the
snapshot history has a season of results (see README).
"""

from dataclasses import dataclass, field
from typing import Callable


def cfb_margin_mass(k: int) -> float:
    k = abs(k)
    keys = {0: 0.0, 1: 0.022, 2: 0.02, 3: 0.085, 4: 0.035, 6: 0.035, 7: 0.065,
            8: 0.025, 10: 0.045, 11: 0.022, 13: 0.02, 14: 0.045, 17: 0.035, 21: 0.03,
            24: 0.022, 28: 0.022}
    if k in keys:
        return keys[k]
    return 0.02 if k <= 20 else 0.012 if k <= 35 else 0.005


def cbb_margin_mass(k: int) -> float:
    k = abs(k)
    if k == 0:
        return 0.0
    return 0.034 if k <= 10 else 0.028 if k <= 16 else 0.018 if k <= 25 else 0.006


def flat_mass(value: float) -> Callable[[int], float]:
    return lambda k: value


@dataclass(frozen=True)
class Sport:
    name: str
    feed_key: str  # The Odds API sport key
    markets: tuple[str, ...]
    margin_mass: Callable[[int], float]
    total_mass: Callable[[int], float]
    key_numbers: tuple[int, ...] = ()
    # Detector thresholds, probability units unless noted.
    steam_move: float = 0.02
    steam_books: int = 3
    steam_window_min: int = 30
    drift_step: float = 0.04
    rlm_move: float = 0.03
    rlm_public_share: float = 0.60
    divergence_ev: float = 0.025
    min_books: int = 3
    sharp_books: tuple[str, ...] = ("pinnacle", "circasports", "lowvig", "betonlineag")
    books: tuple[str, ...] = field(default=(
        "pinnacle", "draftkings", "fanduel", "betmgm", "williamhill_us",
        "espnbet", "betrivers", "fanatics", "bovada", "lowvig",
    ))


SPORTS = {
    "ncaaf": Sport(
        name="ncaaf",
        feed_key="americanfootball_ncaaf",
        markets=("h2h", "spreads", "totals"),
        margin_mass=cfb_margin_mass,
        total_mass=flat_mass(0.016),
        key_numbers=(3, 7, 10, 14, 17),
    ),
    "ncaab": Sport(
        name="ncaab",
        feed_key="basketball_ncaab",
        markets=("h2h", "spreads", "totals"),
        margin_mass=cbb_margin_mass,
        total_mass=flat_mass(0.024),
    ),
    # College baseball trades mostly on the moneyline; run lines sit at +/-1.5.
    "ncaabase": Sport(
        name="ncaabase",
        feed_key="baseball_ncaa",
        markets=("h2h", "totals"),
        margin_mass=flat_mass(0.12),
        total_mass=flat_mass(0.08),
        steam_move=0.025,
        drift_step=0.05,
        divergence_ev=0.03,
        min_books=2,
        steam_books=2,
    ),
}
