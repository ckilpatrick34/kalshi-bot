"""Final scores from ESPN's public scoreboard (no key needed).

The scoreboard is one call per day. Football uses group 80 (FBS games, which
includes FBS-vs-FCS); basketball uses group 50 (Division I). The scoreboard
only carries book lines for games not yet played; a finished game's closing line
comes from its summary page (`summary_line`), which ESPN keeps for roughly the
last year.
"""

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from .prices import american_to_decimal
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


@dataclass(frozen=True)
class BookLine:
    """One book's game lines as ESPN shows them (DraftKings first). For a finished
    game the current numbers are the close. Prices are decimal; spreads are home's."""

    game_id: str
    sport: str
    starts: datetime
    home: str
    away: str
    neutral: bool
    provider: str
    spread_open: float | None
    spread: float | None
    spread_home_price: float | None
    spread_away_price: float | None
    total_open: float | None
    total: float | None
    over_price: float | None
    under_price: float | None
    ml_home: float | None
    ml_away: float | None


def _number(text) -> float | None:
    if text is None:
        return None
    text = str(text).strip().lower().lstrip("ou")
    if text in ("", "off", "-"):
        return None
    if text in ("pk", "pick", "even"):
        return 0.0
    try:
        return float(text)
    except ValueError:
        return None


def _price(text) -> float | None:
    if text is None:
        return None
    text = str(text).strip().lower()
    if text in ("even", "ev"):
        return 2.0
    try:
        return american_to_decimal(float(text))
    except ValueError:
        return None


def parse_lines(payload: dict, sport: str) -> list[BookLine]:
    out = []
    for event in payload.get("events", []):
        comp = (event.get("competitions") or [{}])[0]
        odds = (comp.get("odds") or [None])[0]
        teams = {c.get("homeAway"): c for c in comp.get("competitors", [])}
        if not odds or set(teams) != {"home", "away"}:
            continue
        spread = (odds.get("pointSpread") or {})
        total = (odds.get("total") or {})
        ml = (odds.get("moneyline") or {})

        def leg(block, side, when, field):
            return ((block.get(side) or {}).get(when) or {}).get(field)

        spread_close = _number(leg(spread, "home", "close", "line"))
        if spread_close is None and odds.get("spread") is not None:
            # Older payloads only carry a bare number; the favourite flag gives its sign.
            fav_home = (odds.get("homeTeamOdds") or {}).get("favorite")
            spread_close = -abs(float(odds["spread"])) if fav_home else abs(float(odds["spread"]))
        total_close = _number(leg(total, "over", "close", "line"))
        if total_close is None:
            total_close = _number(odds.get("overUnder"))
        out.append(BookLine(
            game_id=str(event["id"]),
            sport=sport,
            starts=parse_ts(event.get("date") or comp["date"]),
            home=teams["home"]["team"]["displayName"],
            away=teams["away"]["team"]["displayName"],
            neutral=bool(comp.get("neutralSite")),
            provider=(odds.get("provider") or {}).get("name", "unknown"),
            spread_open=_number(leg(spread, "home", "open", "line")),
            spread=spread_close,
            spread_home_price=_price(leg(spread, "home", "close", "odds")),
            spread_away_price=_price(leg(spread, "away", "close", "odds")),
            total_open=_number(leg(total, "over", "open", "line")),
            total=total_close,
            over_price=_price(leg(total, "over", "close", "odds")),
            under_price=_price(leg(total, "under", "close", "odds")),
            ml_home=_price(leg(ml, "home", "close", "odds")),
            ml_away=_price(leg(ml, "away", "close", "odds")),
        ))
    return out


def summary_line(summary: dict, result: "Result") -> BookLine | None:
    """The closing line from a finished game's summary (`pickcenter`), first provider listed."""
    picks = summary.get("pickcenter") or []
    if not picks:
        return None
    pc = picks[0]
    home_odds, away_odds = pc.get("homeTeamOdds") or {}, pc.get("awayTeamOdds") or {}
    spread = _number(pc.get("spread"))
    if spread is not None:
        # ESPN's sign convention varies by endpoint; the favourite flag settles it.
        if home_odds.get("favorite"):
            spread = -abs(spread)
        elif away_odds.get("favorite"):
            spread = abs(spread)
    return BookLine(
        game_id=result.game_id, sport=result.sport, starts=result.played, home=result.home, away=result.away,
        neutral=result.neutral, provider=(pc.get("provider") or {}).get("name", "unknown"),
        spread_open=None, spread=spread,
        spread_home_price=_price(home_odds.get("spreadOdds")), spread_away_price=_price(away_odds.get("spreadOdds")),
        total_open=None, total=_number(pc.get("overUnder")),
        over_price=_price(pc.get("overOdds")), under_price=_price(pc.get("underOdds")),
        ml_home=_price(home_odds.get("moneyLine")), ml_away=_price(away_odds.get("moneyLine")),
    )


class Espn:
    """ESPN scoreboard client. Days that are over are cached on disk when `cache` is set."""

    def __init__(self, session: requests.Session | None = None, cache: str | Path | None = None):
        self.session = session or requests.Session()
        self.cache = Path(cache) if cache else None

    def payload(self, sport: str, day: date) -> dict:
        path, group = PATHS[sport]
        cached = self.cache / sport / f"{day:%Y%m%d}.json" if self.cache else None
        finished = day < (datetime.now(timezone.utc) - timedelta(days=2)).date()
        if cached and finished and cached.exists():
            return json.loads(cached.read_text())
        params = {"dates": day.strftime("%Y%m%d"), "limit": 500}  # above ~800 ESPN silently falls back to 25
        if group:
            params["groups"] = group
        resp = self.session.get(f"{BASE_URL}/{path}/scoreboard", params=params, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        if cached and finished:
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_text(json.dumps(payload))
        return payload

    def summary(self, sport: str, game_id: str) -> dict:
        path, _group = PATHS[sport]
        cached = self.cache / sport / "summary" / f"{game_id}.json" if self.cache else None
        if cached and cached.exists():
            return json.loads(cached.read_text())
        resp = self.session.get(f"{BASE_URL}/{path}/summary", params={"event": game_id}, timeout=30)
        resp.raise_for_status()
        body = resp.json()
        payload = {"pickcenter": body.get("pickcenter") or []}  # keep only what's used; summaries are large
        if cached:
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_text(json.dumps(payload))
        return payload

    def day(self, sport: str, day: date):
        """Completed games, upcoming neutral sites and book lines on one day."""
        payload = self.payload(sport, day)
        return parse_scoreboard(payload, sport), parse_neutral(payload), parse_lines(payload, sport)

    def days(self, sport: str, start: date, end: date):
        day = start
        while day <= end:
            yield day, *self.day(sport, day)
            day += timedelta(days=1)
