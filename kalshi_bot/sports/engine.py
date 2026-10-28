"""One trading cycle across all leagues and venues.

Each cycle:
1. Pull sharp moneylines, devig them, and snapshot fair values to the ledger.
2. Read every venue's game-winner contracts and match them to those games.
3. Update resting orders: mark fills, cancel any whose edge is gone after the sharp
   line moved (the stale-quote risk makers face), reprice ones that were outbid, and
   cancel everything on games about to start. Trading is pre-game only.
4. Place new orders where a contract clears the edge threshold, sized at fractional Kelly.
5. Close out games that have started: record closing fair value and CLV.

In paper mode nothing is sent to a venue. A paper maker order counts as filled once
the venue's ask drops to its price, i.e. a seller would have crossed into it. That
ignores queue position, so paper fills are a little optimistic for maker orders that
sit at the best bid and a little pessimistic for ones that get hit by a seller who
doesn't move the ask.
"""

import logging
from datetime import datetime, timedelta, timezone

from .config import LEAGUES, SportsConfig
from .edge import evaluate, maker_price, still_good
from .ledger import Ledger
from .odds import OddsClient
from .venues import Matched

log = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Engine:
    def __init__(self, cfg: SportsConfig, odds: OddsClient, venues: dict, ledger: Ledger, now=utcnow):
        self.cfg, self.odds, self.venues, self.ledger, self.now = cfg, odds, venues, ledger, now
        self.events: list[str] = []

    def _say(self, msg: str) -> None:
        self.events.append(msg)
        log.info(msg)

    # --- inputs -------------------------------------------------------------

    def matched_contracts(self) -> list[Matched]:
        now = self.now()
        out = []
        for key in self.cfg.leagues:
            league = LEAGUES[key]
            games = self.odds.fair_values(key, league.odds_api_sport, self.cfg.sharp_books, self.cfg.devig_method)
            for g in games:
                self.ledger.record_game(g, now)
            for name, venue in self.venues.items():
                try:
                    out.extend(venue.contracts(league, games))
                except Exception as e:  # one venue being down shouldn't stop the others
                    self._say(f"{name} {key}: could not read markets ({e})")
        return out

    def _tradeable(self, m: Matched) -> bool:
        to_start = m.game.commence - self.now()
        return (
            timedelta(minutes=self.cfg.min_minutes_before_start)
            <= to_start
            <= timedelta(hours=self.cfg.max_hours_before_start)
        )

    # --- order handling -----------------------------------------------------

    def _cancel(self, order, reason: str) -> None:
        if self.cfg.mode == "live" and order["venue_order_id"]:
            venue = self.venues[order["venue"]]
            venue.cancel(order["market_id"], order["venue_order_id"])
            _, filled = venue.status(order["venue_order_id"])
            if filled > 0:
                self.ledger.fill(order["id"], self.now(), filled)
                self._say(f"partial fill kept: {order['team']} {filled:g} @ {order['price']:.2f}")
                return
        self.ledger.cancel(order["id"])
        self._say(f"cancel {order['venue']} {order['team']} @ {order['price']:.2f}: {reason}")

    def _sync_live(self, order) -> bool:
        """Refresh a live order from the venue. Returns True if it is still resting."""
        state, filled = self.venues[order["venue"]].status(order["venue_order_id"])
        if state == "resting":
            return True
        if filled > 0:
            self.ledger.fill(order["id"], self.now(), filled)
            self._say(f"filled {order['venue']} {order['team']} {filled:g} @ {order['price']:.2f}")
        else:
            self.ledger.cancel(order["id"])
        return False

    def manage_resting(self, contracts: list[Matched]) -> set[tuple]:
        """Fill, cancel or keep each resting order. Returns the contracts that still have one."""
        by_key = {(m.contract.venue, m.contract.market_id, m.contract.side): m for m in contracts}
        kept = set()
        for o in self.ledger.resting(self.cfg.mode):
            key = (o["venue"], o["market_id"], o["side"])
            m = by_key.get(key)
            if self.cfg.mode == "live" and o["venue_order_id"] and not self._sync_live(o):
                continue
            if m is None:
                self._cancel(o, "market no longer listed or not matched")
                continue
            if self.cfg.mode == "paper" and m.contract.ask is not None and m.contract.ask <= o["price"] + 1e-9:
                self.ledger.fill(o["id"], self.now())
                self._say(f"paper fill {o['venue']} {o['team']} {o['contracts']:g} @ {o['price']:.2f}")
                continue
            if not self._tradeable(m):
                self._cancel(o, "game starting")
            elif not still_good(m, o["price"], self.cfg):
                self._cancel(o, f"fair moved to {m.fair:.3f}")
            elif self._outbid(m, o["price"]):
                self._cancel(o, "outbid, repricing")
            else:
                kept.add(key)
        return kept

    def _outbid(self, m: Matched, price: float) -> bool:
        """Someone else bids above us and we can still afford to beat them."""
        bid, best = m.contract.bid, maker_price(m, self.cfg)
        return bid is not None and bid > price + 1e-9 and best is not None and best > price + 1e-9

    def place(self, m: Matched, decision) -> None:
        venue_order_id, status = None, "resting" if decision.kind == "maker" else "filled"
        if self.cfg.mode == "live":
            venue_order_id = self.venues[m.contract.venue].place(
                m.contract, decision.price, decision.contracts, post_only=decision.kind == "maker"
            )
            status = "resting"  # live fills are confirmed by the venue on the next sync
        self.ledger.add_order(self.now(), self.cfg.mode, m, decision, status, venue_order_id)
        self._say(
            f"{decision.kind} {self.cfg.mode} {m.contract.venue} {m.team} ({m.game.away} @ {m.game.home}): "
            f"{decision.contracts:g} @ {decision.price:.2f}, fee {decision.fee:.2f}, fair {decision.fair:.3f}, "
            f"edge {decision.edge * 100:.1f}c"
        )

    # --- the cycle ------------------------------------------------------------

    def opportunities(self, contracts: list[Matched]) -> list[tuple[Matched, object]]:
        found = []
        for m in contracts:
            if not self._tradeable(m):
                continue
            d = evaluate(m, self.cfg, self.ledger.game_exposure(m.game.id, self.cfg.mode))
            if d:
                found.append((m, d))
        return found

    def cycle(self, trade: bool = True) -> list[str]:
        self.events = []
        contracts = self.matched_contracts()
        if not trade:
            for m, d in self.opportunities(contracts):
                self._say(
                    f"{m.contract.venue:10} {m.team:28} {d.kind:5} {d.contracts:5g} @ {d.price:.2f} "
                    f"fair {d.fair:.3f} edge {d.edge * 100:.1f}c  ({m.game.away} @ {m.game.home})"
                )
            return self.events
        kept = self.manage_resting(contracts)
        for m in contracts:
            key = (m.contract.venue, m.contract.market_id, m.contract.side)
            if key in kept or not self._tradeable(m):
                continue
            d = evaluate(m, self.cfg, self.ledger.game_exposure(m.game.id, self.cfg.mode))
            if d and d.kind == "taker" and m.contract.ask_size is None and hasattr(self.venues[m.contract.venue], "ask_size"):
                # Polymarket's event listing has no sizes; look up depth only when about to take.
                m.contract.ask_size = self.venues[m.contract.venue].ask_size(m.contract) or 0
                d = evaluate(m, self.cfg, self.ledger.game_exposure(m.game.id, self.cfg.mode))
            if d:
                self.place(m, d)
        closed = self.ledger.close_started_games(self.now())
        if closed:
            self._say(f"recorded closing lines for {closed} game(s)")
        return self.events

    def settle(self) -> list[str]:
        """Grade filled orders on finished games using The Odds API's scores."""
        from .odds import winner_from_score

        self.events = []
        pending = {g["game_id"]: g for g in self.ledger.unsettled_games(self.now())}
        for key in {g["league"] for g in pending.values()}:
            for ev in self.odds.scores(LEAGUES[key].odds_api_sport):
                if ev["id"] in pending and (winner := winner_from_score(ev)):
                    self.ledger.settle(ev["id"], winner)
                    self._say(f"settled {ev['away_team']} @ {ev['home_team']}: {winner} won")
        return self.events
