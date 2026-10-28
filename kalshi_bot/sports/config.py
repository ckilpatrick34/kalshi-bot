"""Settings for the sports engine, read from environment variables.

Everything has a safe default: paper trading, quarter Kelly, pre-game only.
Keys are never read from files in the repo.

    ODDS_API_KEY             The Odds API key (fair value source)
    SPORTS_SHARP_BOOKS       comma list of sharp books to devig, default "pinnacle"
    SPORTS_LEAGUES           comma list from LEAGUES below, default "nfl,nba,mlb,nhl"
    SPORTS_VENUES            comma list of "kalshi", "polymarket", default both. Drop a venue
                             that isn't legal or funded where you live.
    SPORTS_MODE              "paper" (default) or "live"
    SPORTS_BANKROLL          dollars, default 500
    SPORTS_KELLY_FRACTION    default 0.25
    SPORTS_MAKER_MIN_EDGE    fair minus all-in cost needed to rest an order, default 0.015
    SPORTS_TAKER_MIN_EDGE    fair minus all-in cost needed to cross the spread, default 0.03
    SPORTS_MAX_GAME_PCT      worst-case loss allowed on one game, default 0.03 of bankroll
    SPORTS_LEDGER            SQLite path, default data/sports_ledger.sqlite
"""

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class League:
    key: str
    odds_api_sport: str
    kalshi_series: str
    polymarket_league: str


# Kalshi series and Polymarket US league slugs as of Oct 2026. Check them against the
# venues once before trading a league for the first time.
LEAGUES = {
    "nfl": League("nfl", "americanfootball_nfl", "KXNFLGAME", "nfl"),
    "nba": League("nba", "basketball_nba", "KXNBAGAME", "nba"),
    "mlb": League("mlb", "baseball_mlb", "KXMLBGAME", "mlb"),
    "nhl": League("nhl", "icehockey_nhl", "KXNHLGAME", "nhl"),
    "ncaaf": League("ncaaf", "americanfootball_ncaaf", "KXNCAAFGAME", "cfb"),
    "ncaab": League("ncaab", "basketball_ncaab", "KXNCAAMBGAME", "cbb"),
}

VENUES = ("kalshi", "polymarket")


def _list(name: str, default: str) -> list[str]:
    return [x.strip().lower() for x in os.environ.get(name, default).split(",") if x.strip()]


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


@dataclass
class SportsConfig:
    leagues: list[str] = field(default_factory=lambda: ["nfl", "nba", "mlb", "nhl"])
    venues: list[str] = field(default_factory=lambda: list(VENUES))
    sharp_books: list[str] = field(default_factory=lambda: ["pinnacle"])
    mode: str = "paper"
    bankroll: float = 500.0
    kelly_fraction: float = 0.25
    maker_min_edge: float = 0.015
    taker_min_edge: float = 0.03
    max_game_pct: float = 0.03
    max_order_contracts: float = 500
    # Pre-game only: stop quoting this close to the start, and ignore games further out.
    min_minutes_before_start: float = 5.0
    max_hours_before_start: float = 48.0
    devig_method: str = "power"
    ledger_path: str = "data/sports_ledger.sqlite"

    def __post_init__(self):
        unknown = [v for v in self.venues if v not in VENUES]
        if unknown:
            raise ValueError(f"Unknown venues {unknown}; choose from {VENUES}")
        unknown = [lg for lg in self.leagues if lg not in LEAGUES]
        if unknown:
            raise ValueError(f"Unknown leagues {unknown}; choose from {sorted(LEAGUES)}")
        if self.mode not in ("paper", "live"):
            raise ValueError("SPORTS_MODE must be 'paper' or 'live'")

    @classmethod
    def from_env(cls) -> "SportsConfig":
        return cls(
            leagues=_list("SPORTS_LEAGUES", "nfl,nba,mlb,nhl"),
            venues=_list("SPORTS_VENUES", ",".join(VENUES)),
            sharp_books=_list("SPORTS_SHARP_BOOKS", "pinnacle"),
            mode=os.environ.get("SPORTS_MODE", "paper").lower(),
            bankroll=_float("SPORTS_BANKROLL", 500),
            kelly_fraction=_float("SPORTS_KELLY_FRACTION", 0.25),
            maker_min_edge=_float("SPORTS_MAKER_MIN_EDGE", 0.015),
            taker_min_edge=_float("SPORTS_TAKER_MIN_EDGE", 0.03),
            max_game_pct=_float("SPORTS_MAX_GAME_PCT", 0.03),
            ledger_path=os.environ.get("SPORTS_LEDGER", "data/sports_ledger.sqlite"),
        )
