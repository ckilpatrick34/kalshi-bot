"""Settings for the sports picks tool, read from environment variables.

    SPORTS_LEAGUES          comma list from LEAGUES below, default "nfl,nba,mlb,nhl"
    SPORTS_VENUES           comma list of "kalshi", "polymarket", default both. Drop a venue
                            that isn't legal or funded where you live.
    SPORTS_MIN_EV           expected profit per $1 staked, after fees, to flag a play. Default 0.05
    SPORTS_BANKROLL         dollars, used only for the suggested stake. Default 500
    SPORTS_KELLY_FRACTION   default 0.25 (quarter Kelly)
    SPORTS_MAX_GAME_PCT     suggested stake cap per game, share of bankroll. Default 0.03
    SPORTS_DAYS_AHEAD       how far ahead to look for games. Default 3
    SPORTS_HISTORY_YEARS    seasons of results the model learns from. Default 3
    ODDS_API_KEY            optional. When set, each play also shows the sharp books' devigged
                            probability, a useful second opinion on the model.
    SPORTS_SHARP_BOOKS      The Odds API bookmakers for that column, default "pinnacle"
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
# venues once before relying on a league for the first time.
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
    min_ev: float = 0.05
    bankroll: float = 500.0
    kelly_fraction: float = 0.25
    max_game_pct: float = 0.03
    days_ahead: int = 3
    history_years: float = 3.0
    min_team_games: int = 8  # below this a team's rating is mostly a guess, so its plays are marked
    sharp_books: list[str] = field(default_factory=lambda: ["pinnacle"])

    def __post_init__(self):
        unknown = [v for v in self.venues if v not in VENUES]
        if unknown:
            raise ValueError(f"Unknown venues {unknown}; choose from {VENUES}")
        unknown = [lg for lg in self.leagues if lg not in LEAGUES]
        if unknown:
            raise ValueError(f"Unknown leagues {unknown}; choose from {sorted(LEAGUES)}")

    @classmethod
    def from_env(cls) -> "SportsConfig":
        return cls(
            leagues=_list("SPORTS_LEAGUES", "nfl,nba,mlb,nhl"),
            venues=_list("SPORTS_VENUES", ",".join(VENUES)),
            min_ev=_float("SPORTS_MIN_EV", 0.05),
            bankroll=_float("SPORTS_BANKROLL", 500),
            kelly_fraction=_float("SPORTS_KELLY_FRACTION", 0.25),
            max_game_pct=_float("SPORTS_MAX_GAME_PCT", 0.03),
            days_ahead=int(_float("SPORTS_DAYS_AHEAD", 3)),
            history_years=_float("SPORTS_HISTORY_YEARS", 3),
            sharp_books=_list("SPORTS_SHARP_BOOKS", "pinnacle"),
        )
