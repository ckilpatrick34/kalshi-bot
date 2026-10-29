"""Odds feed: The Odds API (https://the-odds-api.com), live and historical.

Cost per live call = markets x regions, where naming up to 10 bookmakers counts as
one region. Three markets over ten books is 3 credits per sport per poll. A
historical snapshot costs 10x that. The key comes from ODDS_API_KEY.

This only reads prices. Nothing here places a bet.
"""

import os
from dataclasses import dataclass
from datetime import datetime

import requests

from .store import iso, parse_ts

BASE_URL = "https://api.the-odds-api.com/v4"


@dataclass(frozen=True)
class Event:
    event_id: str
    sport: str
    commence: datetime
    home: str
    away: str


@dataclass(frozen=True)
class Quote:
    event_id: str
    book: str
    market: str
    point: float | None
    price_a: float
    price_b: float
    book_update: datetime | None


def parse_market(market: dict, home: str, away: str) -> tuple[float | None, float, float] | None:
    """(point, price_a, price_b) for a two-way market, or None if it isn't one."""
    outcomes = {o["name"]: o for o in market.get("outcomes", [])}
    key = market["key"]
    if key in ("h2h", "spreads"):
        if set(outcomes) != {home, away}:
            return None  # a draw outcome or a team-name mismatch
        a, b = outcomes[home], outcomes[away]
        if key == "h2h":
            return None, float(a["price"]), float(b["price"])
        if "point" not in a or "point" not in b or a["point"] != -b["point"]:
            return None
        return float(a["point"]), float(a["price"]), float(b["price"])
    if key == "totals":
        if set(outcomes) != {"Over", "Under"}:
            return None
        a, b = outcomes["Over"], outcomes["Under"]
        if a.get("point") is None or a.get("point") != b.get("point"):
            return None
        return float(a["point"]), float(a["price"]), float(b["price"])
    return None


def parse_events(payload: list[dict], sport: str) -> tuple[list[Event], list[Quote]]:
    events, quotes = [], []
    for e in payload:
        event = Event(e["id"], sport, parse_ts(e["commence_time"]), e["home_team"], e["away_team"])
        events.append(event)
        for book in e.get("bookmakers", []):
            for market in book.get("markets", []):
                parsed = parse_market(market, event.home, event.away)
                if parsed is None:
                    continue
                updated = market.get("last_update") or book.get("last_update")
                quotes.append(Quote(event.event_id, book["key"], market["key"], *parsed,
                                    parse_ts(updated) if updated else None))
    return events, quotes


class OddsApi:
    def __init__(self, api_key: str | None = None, session: requests.Session | None = None):
        self.api_key = api_key or os.environ.get("ODDS_API_KEY")
        if not self.api_key:
            raise RuntimeError("Set ODDS_API_KEY")
        self.session = session or requests.Session()
        self.credits_remaining: int | None = None
        self.last_cost: int | None = None

    def _get(self, path: str, **params):
        resp = self.session.get(f"{BASE_URL}{path}", params={"apiKey": self.api_key, **params}, timeout=30)
        resp.raise_for_status()
        remaining, last = resp.headers.get("x-requests-remaining"), resp.headers.get("x-requests-last")
        self.credits_remaining = int(float(remaining)) if remaining else None
        self.last_cost = int(float(last)) if last else None
        return resp.json()

    def odds(self, feed_key: str, markets, books) -> list[dict]:
        return self._get(f"/sports/{feed_key}/odds", markets=",".join(markets), bookmakers=",".join(books),
                         oddsFormat="decimal", dateFormat="iso")

    def historical_odds(self, feed_key: str, markets, books, at: datetime) -> tuple[datetime, list[dict]]:
        """The snapshot closest to (at or before) `at`; returns its actual timestamp."""
        body = self._get(f"/historical/sports/{feed_key}/odds", markets=",".join(markets),
                         bookmakers=",".join(books), oddsFormat="decimal", dateFormat="iso",
                         date=iso(at).split(".")[0] + "Z")
        return parse_ts(body["timestamp"]), body.get("data", [])
