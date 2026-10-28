"""Sharp-book fair values from The Odds API (https://the-odds-api.com).

Pinnacle is listed in the API's "eu" region. Naming bookmakers directly overrides
the region. Each call costs credits (markets x regions), so the engine polls
every few minutes, not every second. The key comes from ODDS_API_KEY.

This only reads sportsbook prices. Nothing here places a bet anywhere.
"""

import os
from dataclasses import dataclass, field
from datetime import datetime

import requests

from .devig import devig

BASE_URL = "https://api.the-odds-api.com/v4"


@dataclass
class Game:
    id: str
    league: str
    commence: datetime
    home: str
    away: str
    fair: dict[str, float] = field(default_factory=dict)  # team name -> fair win probability
    books: list[str] = field(default_factory=list)

    @property
    def teams(self) -> tuple[str, str]:
        return self.home, self.away


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def fair_from_event(event: dict, league: str, sharp_books: list[str], method: str = "power") -> Game | None:
    """Average the devigged two-way moneyline across whichever sharp books quote it."""
    home, away = event["home_team"], event["away_team"]
    estimates, used = [], []
    for book in event.get("bookmakers", []):
        if book["key"] not in sharp_books:
            continue
        market = next((m for m in book.get("markets", []) if m["key"] == "h2h"), None)
        if not market:
            continue
        prices = {o["name"]: float(o["price"]) for o in market["outcomes"]}
        if set(prices) != {home, away}:
            continue  # a draw outcome or a naming mismatch; moneyline contracts here are two-way
        fair = devig([prices[home], prices[away]], method)
        estimates.append(fair)
        used.append(book["key"])
    if not estimates:
        return None
    p_home = sum(e[0] for e in estimates) / len(estimates)
    return Game(
        id=event["id"],
        league=league,
        commence=parse_time(event["commence_time"]),
        home=home,
        away=away,
        fair={home: p_home, away: 1 - p_home},
        books=used,
    )


class OddsClient:
    def __init__(self, api_key: str | None = None, session: requests.Session | None = None):
        self.api_key = api_key or os.environ.get("ODDS_API_KEY")
        if not self.api_key:
            raise RuntimeError("Set ODDS_API_KEY to read sharp lines")
        self.session = session or requests.Session()
        self.remaining_credits: str | None = None

    def _get(self, path: str, **params) -> list:
        resp = self.session.get(f"{BASE_URL}{path}", params={"apiKey": self.api_key, **params}, timeout=30)
        resp.raise_for_status()
        self.remaining_credits = resp.headers.get("x-requests-remaining")
        return resp.json()

    def moneylines(self, sport: str, sharp_books: list[str]) -> list[dict]:
        return self._get(
            f"/sports/{sport}/odds",
            bookmakers=",".join(sharp_books),
            markets="h2h",
            oddsFormat="decimal",
            dateFormat="iso",
        )

    def scores(self, sport: str, days_from: int = 3) -> list[dict]:
        return self._get(f"/sports/{sport}/scores", daysFrom=days_from, dateFormat="iso")

    def fair_values(self, league: str, sport: str, sharp_books: list[str], method: str = "power") -> list[Game]:
        games = (fair_from_event(e, league, sharp_books, method) for e in self.moneylines(sport, sharp_books))
        return [g for g in games if g is not None]


def winner_from_score(event: dict) -> str | None:
    """Winning team of a completed game from the scores endpoint, or None if not final or tied."""
    if not event.get("completed") or not event.get("scores"):
        return None
    scores = {s["name"]: float(s["score"]) for s in event["scores"]}
    if len(scores) != 2 or len(set(scores.values())) == 1:
        return None
    return max(scores, key=scores.get)
