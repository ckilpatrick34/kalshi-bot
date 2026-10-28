"""Game results and schedules from ESPN's public scoreboard API (no key needed).

Past results train the model; upcoming games are what it prices. Finished months
are cached as CSV under data/results/<league>/YYYY-MM.csv so later runs only
download the current month.
"""

import csv
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

BASE_URL = "https://site.api.espn.com/apis/site/v2/sports"

# ESPN path per league, plus the "groups" filter that limits college to the top division.
ESPN = {
    "nfl": ("football/nfl", None),
    "nba": ("basketball/nba", None),
    "mlb": ("baseball/mlb", None),
    "nhl": ("hockey/nhl", None),
    "ncaaf": ("football/college-football", "80"),
    "ncaab": ("basketball/mens-college-basketball", "50"),
}


@dataclass
class GameResult:
    id: str
    league: str
    start: str  # ISO UTC
    home: str
    away: str
    neutral: bool
    state: str  # "pre", "in" or "post"
    home_score: float | None = None
    away_score: float | None = None

    @property
    def start_time(self) -> datetime:
        return parse_time(self.start)

    @property
    def final(self) -> bool:
        return self.state == "post" and self.home_score is not None and self.away_score is not None


def parse_time(value: str) -> datetime:
    value = value.replace("Z", "+00:00")
    if len(value) == len("2026-01-01T18:00+00:00"):
        value = value[:16] + ":00" + value[16:]  # ESPN leaves out seconds
    return datetime.fromisoformat(value)


def parse_scoreboard(data: dict, league: str) -> list[GameResult]:
    out = []
    for ev in data.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        teams = {c.get("homeAway"): c for c in comp.get("competitors", [])}
        if set(teams) != {"home", "away"}:
            continue
        state = ((comp.get("status") or ev.get("status") or {}).get("type") or {}).get("state", "pre")
        if ((ev.get("status") or {}).get("type") or {}).get("name") in ("STATUS_POSTPONED", "STATUS_CANCELED"):
            continue

        def score(c):
            return float(c["score"]) if state == "post" and c.get("score") not in (None, "") else None

        out.append(
            GameResult(
                id=str(ev["id"]),
                league=league,
                start=parse_time(ev.get("date") or comp["date"]).astimezone(timezone.utc).isoformat(),
                home=teams["home"]["team"]["displayName"],
                away=teams["away"]["team"]["displayName"],
                neutral=bool(comp.get("neutralSite")),
                state=state,
                home_score=score(teams["home"]),
                away_score=score(teams["away"]),
            )
        )
    return out


class ResultsClient:
    def __init__(self, cache_dir: str | Path = "data/results", session: requests.Session | None = None):
        self.cache_dir = Path(cache_dir)
        self.session = session or requests.Session()

    def scoreboard(self, league: str, start: date, end: date) -> list[GameResult]:
        path, groups = ESPN[league]
        params = {"dates": f"{start:%Y%m%d}-{end:%Y%m%d}", "limit": 1000}
        if groups:
            params["groups"] = groups
        resp = self.session.get(f"{BASE_URL}/{path}/scoreboard", params=params, timeout=30)
        resp.raise_for_status()
        return parse_scoreboard(resp.json(), league)

    def _month(self, league: str, first: date, today: date) -> list[GameResult]:
        cache = self.cache_dir / league / f"{first:%Y-%m}.csv"
        last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        finished = last < today
        if finished and cache.exists():
            return read_csv(cache)
        games = []
        # Week-long requests keep each response small (college has hundreds of games a week).
        day = first
        while day <= last:
            games.extend(self.scoreboard(league, day, min(day + timedelta(days=6), last)))
            day += timedelta(days=7)
        if finished:
            write_csv(cache, games)
        return games

    def history(self, league: str, years: float, today: date | None = None) -> list[GameResult]:
        """Every finished game in roughly the last `years` years, oldest first."""
        today = today or datetime.now(timezone.utc).date()
        month = (today - timedelta(days=int(365 * years))).replace(day=1)
        games = []
        while month <= today:
            games.extend(g for g in self._month(league, month, today) if g.final)
            month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
        return sorted({g.id: g for g in games}.values(), key=lambda g: g.start)

    def upcoming(self, league: str, days: int, today: date | None = None) -> list[GameResult]:
        today = today or datetime.now(timezone.utc).date()
        games = self.scoreboard(league, today, today + timedelta(days=days))
        return [g for g in games if g.state == "pre"]


def write_csv(path: Path, games: list[GameResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, [x.name for x in fields(GameResult)])
        w.writeheader()
        w.writerows(asdict(g) for g in games)


def read_csv(path: Path) -> list[GameResult]:
    with open(path, newline="") as f:
        return [
            GameResult(
                id=r["id"], league=r["league"], start=r["start"], home=r["home"], away=r["away"],
                neutral=r["neutral"] == "True", state=r["state"],
                home_score=float(r["home_score"]) if r["home_score"] else None,
                away_score=float(r["away_score"]) if r["away_score"] else None,
            )
            for r in csv.DictReader(f)
        ]
