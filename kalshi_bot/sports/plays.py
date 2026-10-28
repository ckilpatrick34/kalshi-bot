"""Turn model probabilities and venue prices into a ranked list of plays.

For model win probability p and all-in cost c per contract (the ask plus the taker fee):

    edge = p - c                   probability points the price is too low by
    EV   = p / c - 1               expected profit per $1 staked
    stake = Kelly x fraction x bankroll, Kelly = (p - c) / (1 - c), capped per game

The stake is only a suggestion. Nothing here places an order.
"""

from dataclasses import dataclass

from .config import SportsConfig
from .model import Elo
from .odds import Game
from .venues import Matched, taker_fee_per_contract


@dataclass
class Play:
    league: str
    game: str  # "Away @ Home"
    start: str
    team: str
    venue: str
    market_id: str
    price: float  # the ask: what one contract costs before fees
    fee: float  # per contract
    model_p: float
    sharp_p: float | None
    thin_data: bool  # a team in this game has too few games for its rating to mean much

    @property
    def cost(self) -> float:
        return self.price + self.fee

    @property
    def edge(self) -> float:
        return self.model_p - self.cost

    @property
    def ev(self) -> float:
        return self.model_p / self.cost - 1

    def stake(self, cfg: SportsConfig) -> float:
        if self.cost >= 1 or self.edge <= 0:
            return 0.0
        kelly = self.edge / (1 - self.cost)
        return round(min(cfg.kelly_fraction * kelly * cfg.bankroll, cfg.max_game_pct * cfg.bankroll), 2)

    def flagged(self, cfg: SportsConfig) -> bool:
        return self.ev >= cfg.min_ev and not self.thin_data


def model_games(upcoming, elo: Elo) -> list[Game]:
    """Upcoming games with the model's win probability for each team."""
    games = []
    for r in upcoming:
        p = elo.predict(r.home, r.away, r.neutral, r.start_time)
        games.append(Game(r.id, r.league, r.start_time, r.home, r.away, {r.home: p, r.away: 1 - p}, ["elo"]))
    return games


def plays_for(matched: list[Matched], elo: Elo, cfg: SportsConfig, sharp: dict[tuple, float] | None = None) -> list[Play]:
    """One play per contract with an ask. `sharp` maps (home, away, team) to a devigged sharp probability."""
    sharp = sharp or {}
    out = []
    for m in matched:
        c, g = m.contract, m.game
        if c.ask is None or not 0 < c.ask < 1:
            continue
        thin = min(elo.experience(g.home), elo.experience(g.away)) < cfg.min_team_games
        out.append(
            Play(
                league=g.league,
                game=f"{g.away} @ {g.home}",
                start=g.commence.isoformat(),
                team=m.team,
                venue=c.venue,
                market_id=c.market_id,
                price=c.ask,
                fee=round(taker_fee_per_contract(c, c.ask), 4),
                model_p=m.fair,
                sharp_p=sharp.get((g.home, g.away, m.team)),
                thin_data=thin,
            )
        )
    return sorted(out, key=lambda p: p.ev, reverse=True)
