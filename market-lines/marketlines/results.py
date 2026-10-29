"""Final scores from ESPN's public scoreboard (no key needed).

The scoreboard is one call per day. Football uses group 80 (FBS games, which
includes FBS-vs-FCS); basketball uses group 50 (Division I).
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

import requests

from .store import parse_ts

BASE_URL = "https://site.api.espn.com/apis/site/v2/sports"
PATHS = {
    "ncaaf": ("football/college-football", "80"),
    "ncaab": ("basketball/mens-college-basketball", "50"),
    "ncaabase": ("baseball/college-baseball", None),
}


@dataclass(frozen=True)
class Result:
    game_id: str
    sport: str
    played: datetime
    home: str
    away: str
    home_pts: int
    away_pts: int
    neutral: bool

    @property
    def margin(self) -> int:
        return self.home_pts - self.away_pts

    @property
    def total(self) -> int:
        return self.home_pts + self.away_pts


def parse_scoreboard(payload: dict, sport: str) -> list[Result]:
    out = []
    for event in payload.get("events", []):
        comp = (event.get("competitions") or [{}])[0]
        if not comp.get("status", event.get("status", {})).get("type", {}).get("completed"):
            continue
        teams = {c.get("homeAway"): c for c in comp.get("competitors", [])}
        if set(teams) != {"home", "away"}:
            continue
        try:
            home_pts, away_pts = int(teams["home"]["score"]), int(teams["away"]["score"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append(Result(
            game_id=str(event["id"]),
            sport=sport,
            played=parse_ts(event.get("date") or comp["date"]),
            home=teams["home"]["team"]["displayName"],
            away=teams["away"]["team"]["displayName"],
            home_pts=home_pts,
            away_pts=away_pts,
            neutral=bool(comp.get("neutralSite")),
        ))
    return out


def parse_neutral(payload: dict) -> list[tuple[str, str, datetime]]:
    """(home, away, start) for games not yet played at neutral sites; the odds feed doesn't say."""
    out = []
    for event in payload.get("events", []):
        comp = (event.get("competitions") or [{}])[0]
        if not comp.get("neutralSite") or comp.get("status", {}).get("type", {}).get("completed"):
            continue
        teams = {c.get("homeAway"): c for c in comp.get("competitors", [])}
        if set(teams) == {"home", "away"}:
            out.append((teams["home"]["team"]["displayName"], teams["away"]["team"]["displayName"],
                        parse_ts(event.get("date") or comp["date"])))
    return out


class Espn:
    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()

    def day(self, sport: str, day: date) -> tuple[list[Result], list[tuple[str, str, datetime]]]:
        """Completed games and upcoming neutral-site games on one day."""
        path, group = PATHS[sport]
        params = {"dates": day.strftime("%Y%m%d"), "limit": 1000}
        if group:
            params["groups"] = group
        resp = self.session.get(f"{BASE_URL}/{path}/scoreboard", params=params, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        return parse_scoreboard(payload, sport), parse_neutral(payload)

    def days(self, sport: str, start: date, end: date):
        day = start
        while day <= end:
            yield day, *self.day(sport, day)
            day += timedelta(days=1)
