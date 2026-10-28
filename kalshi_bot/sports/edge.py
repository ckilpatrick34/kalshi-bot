"""Decide whether a contract is worth buying, at what price, and how many.

For fair win probability p and all-in cost c per contract (price plus fee):

    edge  = p - c
    Kelly = (p - c) / (1 - c) of bankroll, scaled down by the Kelly fraction

Resting a maker order is preferred: it pays no fee on most series, so the engine
bids one cent above the current best bid, as long as that still leaves at least
`maker_min_edge`. Crossing the spread as a taker needs the larger `taker_min_edge`
after the taker fee. Stakes are capped so the worst-case loss on one game, across
all venues and both teams, stays under `max_game_pct` of bankroll.
"""

import math
from dataclasses import dataclass

from .config import SportsConfig
from .venues import Matched, fee

TICK = 0.01


@dataclass
class Decision:
    kind: str  # "maker" or "taker"
    price: float
    contracts: float
    fee: float
    fair: float

    @property
    def cost_per_contract(self) -> float:
        return self.price + self.fee / self.contracts

    @property
    def edge(self) -> float:
        return self.fair - self.cost_per_contract


def kelly_fraction(p: float, cost: float) -> float:
    if not 0 < cost < 1:
        return 0.0
    return max(0.0, (p - cost) / (1 - cost))


def _size(m: Matched, kind: str, price: float, cfg: SportsConfig, game_exposure: float, cap: float | None):
    """Contracts to buy at `price`, or None if the edge after the rounded fee is gone."""
    p = m.fair
    per_contract_fee = fee(m.contract, kind, price, 1000) / 1000  # fee rate before per-order rounding
    f = kelly_fraction(p, price + per_contract_fee)
    budget = min(cfg.kelly_fraction * f * cfg.bankroll, cfg.max_game_pct * cfg.bankroll - game_exposure)
    if budget <= 0:
        return None
    n = math.floor(budget / (price + per_contract_fee))
    n = min(n, cfg.max_order_contracts, math.floor(cap) if cap is not None else n)
    while n >= 1:
        total_fee = fee(m.contract, kind, price, n)
        min_edge = cfg.maker_min_edge if kind == "maker" else cfg.taker_min_edge
        if p - (price + total_fee / n) >= min_edge:
            return Decision(kind, round(price, 2), n, total_fee, p)
        if n == 1:
            return None
        n = max(1, n // 2)  # rounding a small order's fee up to a cent can eat the edge
    return None


def _maker_edge(m: Matched, price: float) -> float:
    return m.fair - (price + fee(m.contract, "maker", price, 1000) / 1000)


def maker_price(m: Matched, cfg: SportsConfig) -> float | None:
    """Highest bid that keeps the maker edge, improves the book by at most one tick and can't cross."""
    c = m.contract
    price = math.floor((m.fair - cfg.maker_min_edge) / TICK + 1e-9) * TICK
    while price >= TICK and _maker_edge(m, price) < cfg.maker_min_edge:
        price -= TICK
    if c.bid is not None:
        price = min(price, c.bid + TICK)
    if c.ask is not None:
        price = min(price, c.ask - TICK)
    price = round(price, 2)
    return price if price >= TICK else None


def evaluate(m: Matched, cfg: SportsConfig, game_exposure: float) -> Decision | None:
    c = m.contract
    if c.ask is not None:
        taker = _size(m, "taker", c.ask, cfg, game_exposure, c.ask_size)
        if taker:
            return taker
    price = maker_price(m, cfg)
    if price is None:
        return None
    return _size(m, "maker", price, cfg, game_exposure, None)


def still_good(m: Matched, price: float, cfg: SportsConfig) -> bool:
    """Whether a resting bid at `price` still clears the maker edge against the latest fair value."""
    return _maker_edge(m, price) >= cfg.maker_min_edge
