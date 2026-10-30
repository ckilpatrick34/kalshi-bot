"""Game-day board: Kalshi college football prices against the sportsbook moneyline.

For every game on a date, takes the DraftKings moneyline that ESPN's public scoreboard
shows, removes the book's margin to get a fair win probability, and compares it with
the cheapest way to back each team on Kalshi (YES on that team, or NO on the other
one). A play is flagged when the fair probability beats the Kalshi ask plus the taker
fee by at least `min_ev` per dollar staked.

Advice and paper tracking only: nothing here sends an order. Needs no keys.

The book line is a reference, not the truth. DraftKings carries a margin and isn't the
sharpest book, so small gaps are noise. The edge worth having is a resting limit order
on Kalshi below fair, which `limit_price` suggests.
"""

import json
import math
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from .fees import taker_fee

EASTERN = ZoneInfo("America/New_York")
ESPN_URL = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard"
ESPN_GROUPS = (80, 81)  # FBS, FCS
SERIES = "KXNCAAFGAME"
# Kalshi lists some games with a placeholder time (and date) until kickoff is set, so
# teams decide the match and time only has to be within a few days.
MATCH_WINDOW = timedelta(days=3)


# --- sportsbook side --------------------------------------------------------


def american_to_prob(odds: str | float) -> float | None:
    """Implied probability of American odds such as "-150", "+2000" or "EVEN"."""
    text = str(odds).strip().upper()
    if text in ("EVEN", "EV", "PK"):
        return 0.5
    try:
        a = float(text)
    except ValueError:
        return None
    if abs(a) < 100:
        return None
    return 100 / (a + 100) if a > 0 else -a / (-a + 100)


def devig_power(probs: list[float]) -> list[float]:
    """Remove the book's margin: find k with sum(p ** k) == 1.

    Takes more margin off the longshot than the favourite, as books shade it."""
    if sum(probs) <= 1:
        total = sum(probs)
        return [p / total for p in probs]
    lo, hi = 1.0, 10.0
    for _ in range(100):
        k = (lo + hi) / 2
        lo, hi = (k, hi) if sum(p**k for p in probs) > 1 else (lo, k)
    fair = [p ** ((lo + hi) / 2) for p in probs]
    return [p / sum(fair) for p in fair]


@dataclass
class BookGame:
    id: str
    start: pd.Timestamp
    home: str
    away: str
    neutral: bool
    aliases: dict[str, list[str]]  # "home"/"away" -> names and abbreviation
    fair: dict[str, float] | None  # "home"/"away" -> devigged win probability
    line: str  # e.g. "ND -30.5", for display
    book: str


def parse_scoreboard(payload: dict) -> list[BookGame]:
    games = []
    for event in payload.get("events", []):
        comp = event["competitions"][0]
        if comp.get("status", {}).get("type", {}).get("state", "pre") != "pre":
            continue
        teams, aliases = {}, {}
        for c in comp["competitors"]:
            t = c["team"]
            teams[c["homeAway"]] = t.get("location") or t["displayName"]
            aliases[c["homeAway"]] = [x for x in (t.get("location"), t.get("shortDisplayName"),
                                                  t.get("displayName"), t.get("abbreviation")) if x]
        odds = (comp.get("odds") or [{}])[0]
        ml = odds.get("moneyline") or {}
        raw = {side: american_to_prob(((ml.get(side) or {}).get("close") or {}).get("odds", ""))
               for side in ("home", "away")}
        fair = None
        if raw["home"] and raw["away"]:
            h, a = devig_power([raw["home"], raw["away"]])
            fair = {"home": h, "away": a}
        games.append(BookGame(
            id=str(event["id"]), start=pd.Timestamp(event["date"]), home=teams["home"], away=teams["away"],
            neutral=bool(comp.get("neutralSite")), aliases=aliases, fair=fair,
            line=odds.get("details", ""), book=(odds.get("provider") or {}).get("name", ""),
        ))
    return games


def fetch_book_games(day: date, session: requests.Session | None = None) -> list[BookGame]:
    """Every FBS and FCS game on `day` (Eastern), one request per division.

    ESPN rejects date ranges for college football with a division filter, so this asks
    for one day at a time."""
    session = session or requests.Session()
    out = {}
    for d in (day, day + timedelta(days=1)):  # late Eastern kickoffs are listed under the next UTC day
        for group in ESPN_GROUPS:
            resp = session.get(ESPN_URL, params={"dates": f"{d:%Y%m%d}", "groups": group, "limit": 400}, timeout=30)
            resp.raise_for_status()
            for g in parse_scoreboard(resp.json()):
                if g.start.tz_convert(EASTERN).date() == day:
                    out[g.id] = g
    return sorted(out.values(), key=lambda g: g.start)


# --- Kalshi side ------------------------------------------------------------


def _dollars(m: dict, key: str) -> float | None:
    v = m.get(f"{key}_dollars")
    if v in (None, ""):
        v = m.get(key)
        return None if v in (None, "") else float(v) / 100
    return float(v)


@dataclass
class KalshiTeam:
    ticker: str
    event_ticker: str
    team: str
    code: str
    yes_bid: float | None
    yes_ask: float | None
    no_ask: float | None
    yes_ask_size: float
    no_ask_size: float
    volume: float
    when: pd.Timestamp


def kalshi_team(m: dict) -> KalshiTeam:
    when = m.get("occurrence_datetime") or m.get("expected_expiration_time") or m["close_time"]
    return KalshiTeam(
        ticker=m["ticker"], event_ticker=m["event_ticker"], team=m.get("yes_sub_title") or m["title"],
        code=m["ticker"].rsplit("-", 1)[-1], yes_bid=_dollars(m, "yes_bid"), yes_ask=_dollars(m, "yes_ask"),
        no_ask=_dollars(m, "no_ask"), yes_ask_size=float(m.get("yes_ask_size_fp") or 0),
        no_ask_size=float(m.get("no_ask_size_fp") or 0), volume=float(m.get("volume_fp") or m.get("volume") or 0),
        when=pd.Timestamp(when),
    )


def kalshi_games(markets: list[dict], day: date) -> dict[str, list[KalshiTeam]]:
    """Two-team events whose game falls on `day` in Eastern time (with a little slack)."""
    events: dict[str, list[KalshiTeam]] = {}
    for m in markets:
        t = kalshi_team(m)
        if abs(t.when.tz_convert(EASTERN).date() - day) <= timedelta(days=2):
            events.setdefault(t.event_ticker, []).append(t)
    return {e: teams for e, teams in events.items() if len(teams) == 2}


# --- matching ---------------------------------------------------------------


def normalize(name: str) -> str:
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    name = name.replace("&", " and ").replace(".", "").replace("'", "")
    words = re.sub(r"[^a-z0-9 ]", " ", name).split()
    # "Ohio St." -> "ohio state", but leave "St. Thomas" alone.
    words = [("state" if w == "st" and i > 0 else w) for i, w in enumerate(words)]
    return " ".join(words)


def team_score(team: KalshiTeam, aliases: list[str]) -> float:
    k = normalize(team.team)
    best = 0.0
    for alias in aliases:
        a = normalize(alias)
        if k == a or team.code.lower() == a.replace(" ", ""):
            return 1.0
        best = max(best, SequenceMatcher(None, k, a).ratio())
    return best


MIN_MATCH = 0.85


def match_games(kalshi: dict[str, list[KalshiTeam]], book: list[BookGame]):
    """Pair each Kalshi event with a book game. Yields (game, {"home": KalshiTeam, "away": KalshiTeam}).

    A pairing needs both teams to match clearly; anything unclear is left out."""
    used = set()
    for event, (a, b) in kalshi.items():
        best = None
        for g in book:
            if g.id in used or abs(a.when - g.start) > MATCH_WINDOW:
                continue
            straight = (team_score(a, g.aliases["home"]), team_score(b, g.aliases["away"]))
            crossed = (team_score(a, g.aliases["away"]), team_score(b, g.aliases["home"]))
            for scores, sides in ((straight, {"home": a, "away": b}), (crossed, {"home": b, "away": a})):
                gap = abs(a.when - g.start).total_seconds()
                if min(scores) >= MIN_MATCH and (best is None or (sum(scores), -gap) > best[0]):
                    best = ((sum(scores), -gap), g, sides)
        if best:
            used.add(best[1].id)
            yield best[1], best[2]


# --- plays ------------------------------------------------------------------


@dataclass
class Settings:
    min_ev: float = 0.03  # expected profit per $1, after the taker fee, to flag a play
    bankroll: float = 200.0
    kelly_fraction: float = 0.25
    max_bet: float = 10.0  # dollars per play
    day_cap: float = 50.0  # dollars across all paper plays in one board run
    min_price: float = 0.08  # skip deep longshots and near-locks: tiny edges, big variance
    max_price: float = 0.92
    min_volume: float = 100.0  # contracts traded; thinner markets have unreliable asks


@dataclass
class Play:
    game: str
    start: str
    team: str
    line: str
    ticker: str
    side: str  # "yes" or "no" on `ticker`
    fair: float
    ask: float
    ask_size: float
    volume: float
    fee_per_contract: float
    ev: float  # expected profit per $1 at the ask, after the taker fee
    limit_price: float  # highest resting bid that still clears min_ev (makers pay no fee here)
    contracts: int = 0
    stake: float = 0.0
    flagged: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def cost(self) -> float:
        return self.ask + self.fee_per_contract


def kelly_contracts(p: float, cost: float, s: Settings, room: float) -> int:
    """Quarter-Kelly contracts on the bankroll, capped per play and by what's left of the day cap."""
    if cost >= 1 or p <= cost:
        return 0
    f = s.kelly_fraction * (p - cost) / (1 - cost)
    dollars = min(f * s.bankroll, s.max_bet, room)
    return max(0, math.floor(dollars / cost))


def best_way_to_back(team: KalshiTeam, other: KalshiTeam) -> tuple[str, str, float | None, float]:
    """YES on the team, or NO on the other team, whichever is cheaper right now."""
    options = []
    if team.yes_ask:
        options.append((team.ticker, "yes", team.yes_ask, team.yes_ask_size))
    if other.no_ask:
        options.append((other.ticker, "no", other.no_ask, other.no_ask_size))
    if not options:
        return team.ticker, "yes", None, 0.0
    return min(options, key=lambda o: o[2])


def build_plays(pairs, s: Settings) -> list[Play]:
    plays = []
    for g, sides in pairs:
        if not g.fair:
            continue
        label = f"{g.away} {'vs' if g.neutral else '@'} {g.home}"
        start = g.start.tz_convert(EASTERN).strftime("%a %I:%M%p")
        for side, other in (("home", "away"), ("away", "home")):
            team, opp = sides[side], sides[other]
            ticker, how, ask, size = best_way_to_back(team, opp)
            if ask is None:
                continue
            fair = g.fair[side]
            fee = taker_fee(ask, 100) / 100  # per contract, at a 100-lot so rounding doesn't dominate
            ev = fair / (ask + fee) - 1
            limit = math.floor(fair / (1 + s.min_ev) * 100) / 100
            p = Play(label, start, g.home if side == "home" else g.away, g.line, ticker, how, round(fair, 4),
                     ask, size, team.volume + opp.volume, fee, round(ev, 4), limit)
            if not s.min_price <= ask <= s.max_price:
                p.notes.append("price outside range")
            if p.volume < s.min_volume:
                p.notes.append("thin market")
            plays.append(p)
    return sorted(plays, key=lambda p: p.ev, reverse=True)


def size_plays(plays: list[Play], s: Settings) -> list[Play]:
    """Flag plays that clear min_ev and pass the filters, and size them inside the caps."""
    room = s.day_cap
    games_taken = set()
    for p in plays:
        if p.ev < s.min_ev or p.notes or p.game in games_taken:
            continue
        n = min(kelly_contracts(p.fair, p.cost, s, room), int(p.ask_size) if p.ask_size else 0)
        if n <= 0:
            continue
        p.flagged, p.contracts, p.stake = True, n, round(n * p.cost, 2)
        room -= p.stake
        games_taken.add(p.game)  # one side per game
    return plays


def next_game_day(today: date | None = None) -> date:
    """Today if it's Saturday, otherwise the coming Saturday."""
    today = today or datetime.now(EASTERN).date()
    return today + timedelta(days=(5 - today.weekday()) % 7)


# --- paper ledger -----------------------------------------------------------


def record(ledger: Path, plays: list[Play]) -> int:
    """Append flagged plays as paper bets, skipping tickers already open in the ledger."""
    ledger.parent.mkdir(parents=True, exist_ok=True)
    open_keys = {(b["ticker"], b["side"]) for b in read_ledger(ledger) if b.get("status") == "open"}
    n = 0
    with open(ledger, "a") as f:
        for p in plays:
            if p.flagged and (p.ticker, p.side) not in open_keys:
                row = {"placed": datetime.now(EASTERN).isoformat(timespec="seconds"), "status": "open",
                       **{k: v for k, v in asdict(p).items() if k != "notes"}}
                f.write(json.dumps(row) + "\n")
                n += 1
    return n


def read_ledger(ledger: Path) -> list[dict]:
    if not ledger.exists():
        return []
    return [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()]


def settle(ledger: Path, result_of) -> list[dict]:
    """Settle open paper bets whose market has a result. `result_of(ticker)` returns "yes", "no" or None."""
    bets = read_ledger(ledger)
    for b in bets:
        if b["status"] != "open":
            continue
        result = result_of(b["ticker"])
        if result not in ("yes", "no"):
            continue
        fee = taker_fee(b["ask"], b["contracts"])
        won = result == b["side"]
        b.update(status="won" if won else "lost", result=result,
                 pnl=round(b["contracts"] * ((1 - b["ask"]) if won else -b["ask"]) - fee, 2))
    ledger.write_text("".join(json.dumps(b) + "\n" for b in bets))
    return bets
