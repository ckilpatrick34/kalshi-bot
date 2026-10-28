"""Read game-winner contracts from each venue and send orders to it.

Every contract is "this team wins, pays $1". The engine only ever buys such a
contract; it never sells one it doesn't own.

* Kalshi lists one market per team in each game event (KXNFLGAME-26SEP09NESEA has a
  New England market and a Seattle market). Buying a team means a YES bid on its market.
* Polymarket US lists one moneyline market per game. Its YES ("long") side is one team
  and NO ("short") is the other, so the second team's ask is 1 - the long bid.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from ..client import KalshiClient
from ..fees import maker_fee, polymarket_us_taker_fee, taker_fee
from ..polymarket_us import PolymarketUSClient, amount
from .odds import Game, parse_time
from .teams import match_pair

EASTERN = ZoneInfo("America/New_York")
MONTHS = {m: i for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1)}


@dataclass
class Contract:
    venue: str
    market_id: str  # Kalshi ticker or Polymarket US market slug
    team: str  # the venue's name for the team this contract pays on
    side: str  # "yes" (Kalshi), "long" or "short" (Polymarket US)
    bid: float | None
    ask: float | None
    ask_size: float | None = None
    fee_type: str = "quadratic"
    fee_multiplier: float = 1.0


@dataclass
class Matched:
    contract: Contract
    game: Game
    team: str  # the Odds API's name for the same team

    @property
    def fair(self) -> float:
        return self.game.fair[self.team]


def fee(contract: Contract, kind: str, price: float, count: float) -> float:
    if contract.venue == "kalshi":
        if kind == "taker":
            return taker_fee(price, count, contract.fee_multiplier)
        return maker_fee(price, count, contract.fee_type)
    if contract.venue == "polymarket":
        return polymarket_us_taker_fee(price, count) if kind == "taker" else 0.0
    raise ValueError(contract.venue)


def _closest_game(games: list[Game], names: tuple[str, str], when: datetime | date, max_gap: timedelta):
    """The game with these two teams nearest in time to `when`, with the venue->odds name mapping."""
    best = None
    for g in games:
        mapping = match_pair(names, g.teams)
        if not mapping:
            continue
        if isinstance(when, datetime):
            gap = abs(g.commence - when)
        else:
            gap = timedelta(days=abs((g.commence.astimezone(EASTERN).date() - when).days))
        if gap <= max_gap and (best is None or gap < best[0]):
            best = (gap, g, mapping)
    return (best[1], best[2]) if best else (None, None)


# --- Kalshi -----------------------------------------------------------------


def kalshi_event_date(event_ticker: str) -> date | None:
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})", event_ticker)
    if not m or m.group(2) not in MONTHS:
        return None
    return date(2000 + int(m.group(1)), MONTHS[m.group(2)], int(m.group(3)))


def _dollars(m: dict, name: str) -> float | None:
    v = m.get(f"{name}_dollars")
    if v not in (None, ""):
        return float(v)
    if m.get(name) is not None:
        return m[name] / 100
    return None


def kalshi_contracts(markets: list[dict], games: list[Game], fee_type: str, fee_multiplier: float) -> list[Matched]:
    events: dict[str, list[dict]] = {}
    for m in markets:
        events.setdefault(m["event_ticker"], []).append(m)
    out = []
    for event_ticker, ms in events.items():
        when = kalshi_event_date(event_ticker)
        if len(ms) != 2 or when is None:
            continue  # tie markets (soccer) or an unexpected layout
        names = (ms[0].get("yes_sub_title") or "", ms[1].get("yes_sub_title") or "")
        game, mapping = _closest_game(games, names, when, timedelta(days=0))
        if not game:
            continue
        for m in ms:
            bid, ask = _dollars(m, "yes_bid"), _dollars(m, "yes_ask")
            size = float(m.get("yes_ask_size_fp") or 0) or None
            contract = Contract(
                "kalshi", m["ticker"], m["yes_sub_title"], "yes",
                bid if bid else None, ask if ask and ask < 1 else None, size, fee_type, fee_multiplier,
            )
            out.append(Matched(contract, game, mapping[m["yes_sub_title"]]))
    return out


class KalshiVenue:
    name = "kalshi"

    def __init__(self, client: KalshiClient | None = None):
        # Market data always comes from prod; orders go to KALSHI_ENV (demo unless changed).
        self.data = KalshiClient(env="prod")
        self.trading = client or KalshiClient()
        self._series: dict[str, dict] = {}

    def contracts(self, league, games: list[Game]) -> list[Matched]:
        if league.kalshi_series not in self._series:
            self._series[league.kalshi_series] = self.data.get_series(league.kalshi_series)
        s = self._series[league.kalshi_series]
        markets = list(self.data.markets(series_ticker=league.kalshi_series, status="open"))
        return kalshi_contracts(markets, games, s.get("fee_type", "quadratic"), float(s.get("fee_multiplier", 1)))

    def place(self, contract: Contract, price: float, count: float, post_only: bool) -> str:
        resp = self.trading.create_order(contract.market_id, "bid", count, price, post_only=post_only)
        order = resp.get("order", resp)
        return order.get("order_id") or order.get("id")

    def status(self, venue_order_id: str) -> tuple[str, float]:
        o = self.trading.get_order(venue_order_id)
        filled = float(o.get("fill_count_fp") or o.get("fill_count") or 0)
        state = {"resting": "resting", "executed": "filled"}.get(o.get("status"), "cancelled")
        return state, filled

    def cancel(self, contract_market_id: str, venue_order_id: str) -> None:
        self.trading.cancel_order(venue_order_id)


# --- Polymarket US ----------------------------------------------------------


def _side_team(side: dict, teams_by_id: dict) -> str:
    team = teams_by_id.get(side.get("teamId")) or side.get("team") or {}
    return team.get("name") or side.get("description") or ""


def polymarket_contracts(events: list[dict], games: list[Game], bbo=None) -> list[Matched]:
    out = []
    for ev in events:
        if ev.get("live") or ev.get("ended") or not ev.get("startTime"):
            continue
        market = next((m for m in ev.get("markets", []) if m.get("sportsMarketType") == "SPORTS_MARKET_TYPE_MONEYLINE"), None)
        if not market or len(market.get("marketSides", [])) != 2:
            continue
        teams_by_id = {t.get("id"): t for t in ev.get("teams", [])}
        sides = market["marketSides"]
        long_side = next((s for s in sides if s.get("long")), sides[0])
        short_side = next(s for s in sides if s is not long_side)
        long_team, short_team = _side_team(long_side, teams_by_id), _side_team(short_side, teams_by_id)
        game, mapping = _closest_game(games, (long_team, short_team), parse_time(ev["startTime"]), timedelta(hours=6))
        if not game:
            continue
        bid, ask = amount(market.get("bestBidQuote")), amount(market.get("bestAskQuote"))
        if (bid is None or ask is None) and bbo:
            data = bbo(market["slug"])
            bid, ask = amount(data.get("bestBid")), amount(data.get("bestAsk"))
        out.append(Matched(Contract("polymarket", market["slug"], long_team, "long", bid, ask), game, mapping[long_team]))
        out.append(
            Matched(
                Contract(
                    "polymarket", market["slug"], short_team, "short",
                    round(1 - ask, 4) if ask is not None else None,
                    round(1 - bid, 4) if bid is not None else None,
                ),
                game,
                mapping[short_team],
            )
        )
    return out


class PolymarketVenue:
    name = "polymarket"

    def __init__(self, client: PolymarketUSClient | None = None):
        self.client = client or PolymarketUSClient()

    def contracts(self, league, games: list[Game]) -> list[Matched]:
        return polymarket_contracts(self.client.league_events(league.polymarket_league), games, self.client.bbo)

    def ask_size(self, contract: Contract) -> float | None:
        data = self.client.bbo(contract.market_id)
        shares = data.get("askShares" if contract.side == "long" else "bidShares")
        return float(shares) if shares not in (None, "") else None

    def place(self, contract: Contract, price: float, count: float, post_only: bool) -> str:
        # BUY_SHORT is priced as the NO contract's price, the same way this engine prices it.
        intent = "ORDER_INTENT_BUY_LONG" if contract.side == "long" else "ORDER_INTENT_BUY_SHORT"
        return self.client.create_order(contract.market_id, intent, price, count, post_only)["id"]

    def status(self, venue_order_id: str) -> tuple[str, float]:
        o = self.client.get_order(venue_order_id)
        filled = float(o.get("cumQuantity") or 0)
        state = o.get("state", "")
        if state in ("ORDER_STATE_NEW", "ORDER_STATE_PARTIALLY_FILLED", "ORDER_STATE_PENDING_NEW", "ORDER_STATE_PENDING_RISK"):
            return "resting", filled
        return ("filled" if state == "ORDER_STATE_FILLED" else "cancelled"), filled

    def cancel(self, contract_market_id: str, venue_order_id: str) -> None:
        self.client.cancel_order(venue_order_id, contract_market_id)


def build_venues(names: list[str]) -> dict:
    factories = {"kalshi": KalshiVenue, "polymarket": PolymarketVenue}
    return {n: factories[n]() for n in names}
