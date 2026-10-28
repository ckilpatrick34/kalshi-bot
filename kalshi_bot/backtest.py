"""Event-driven backtester that replays Kalshi trades.

Kalshi doesn't publish order book history, so fills are inferred from trades:

* Maker orders rest on the YES book ("bid" buys YES, "ask" sells YES, which is the
  same as buying NO at 1 - price). A resting bid at p fills when a later trade has
  a taker selling YES (taker_side "no") at a price <= p. Trading through our price
  fills up to the trade's size; trading exactly at our price fills only
  `queue_fill_fraction` of it, because others were in the queue ahead of us.
* Taker orders fill immediately at the last trade price plus `taker_slippage`,
  capped at the size of that last trade, and pay the taker fee.

Markets settle at close_time at $1 (result "yes") or $0 per YES contract.
"""

from dataclasses import dataclass, field
from itertools import count as counter

import pandas as pd

from .fees import maker_fee, taker_fee
from .risk import RiskLimits, RiskManager


@dataclass
class Order:
    id: int
    ticker: str
    side: str  # "bid" or "ask" on the YES book
    price: float
    count: float
    kind: str  # "maker" or "taker"
    placed: pd.Timestamp
    remaining: float = 0.0

    def __post_init__(self):
        self.remaining = self.count


@dataclass
class Fill:
    ts: pd.Timestamp
    ticker: str
    side: str
    price: float
    count: float
    fee: float
    kind: str


@dataclass
class BacktestConfig:
    starting_cash: float = 500.0
    queue_fill_fraction: float = 0.5
    taker_slippage: float = 0.01
    limits: RiskLimits = field(default_factory=RiskLimits)


def _worst_case_loss(pos: float, cash_flow: float) -> float:
    """Largest loss this market can still cause: settles YES (+pos dollars) or NO (+0)."""
    return max(0.0, -min(cash_flow + pos, cash_flow))


class Context:
    """What a strategy sees and can do. Strategies call these from their callbacks."""

    def __init__(self, engine: "Backtester"):
        self._e = engine

    @property
    def now(self) -> pd.Timestamp:
        return self._e.now

    def market(self, ticker: str) -> dict:
        return self._e.markets[ticker]

    def last_price(self, ticker: str) -> float | None:
        return self._e.last_price.get(ticker)

    def position(self, ticker: str) -> float:
        return self._e.pos.get(ticker, 0.0)

    def orders(self, ticker: str) -> list[Order]:
        return [o for o in self._e.resting.values() if o.ticker == ticker]

    def place(self, ticker: str, side: str, price: float, count: float, kind: str = "maker") -> Order | None:
        return self._e.place(ticker, side, price, count, kind)

    def cancel(self, order: Order) -> None:
        self._e.resting.pop(order.id, None)


class Strategy:
    def on_trade(self, ctx: Context, trade) -> None:  # trade is a row with ts, ticker, price, count, taker_side
        pass

    def on_settle(self, ctx: Context, ticker: str, result: str) -> None:
        pass


class Backtester:
    def __init__(self, markets: pd.DataFrame, trades: pd.DataFrame, config: BacktestConfig | None = None):
        self.config = config or BacktestConfig()
        self.markets = {r["ticker"]: r for r in markets.to_dict("records")}
        self.trades = trades[trades["ticker"].isin(self.markets)].sort_values("ts", kind="stable")
        self.cash = self.config.starting_cash
        self.pos: dict[str, float] = {}
        self.cash_flow: dict[str, float] = {}  # per open market, fees included
        self.last_price: dict[str, float] = {}
        self.last_size: dict[str, float] = {}
        self.resting: dict[int, Order] = {}
        self.fills: list[Fill] = []
        self.settlements: list[dict] = []
        self.equity_curve: list[tuple] = []
        self.risk = RiskManager(self.config.limits, self.config.starting_cash)
        self.now = None
        self._ids = counter(1)

    # --- accounting -------------------------------------------------------

    def equity(self) -> float:
        marked = sum(p * self.last_price.get(t, 0.0) for t, p in self.pos.items())
        return self.cash + marked

    def _exposure_after(self, ticker: str, side: str, price: float, count: float) -> tuple[float, float]:
        """Worst-case loss in this market and its event if every resting order and this one filled."""

        def market_loss(t: str, extra: list[tuple]) -> float:
            pos, cf = self.pos.get(t, 0.0), self.cash_flow.get(t, 0.0)
            worst = _worst_case_loss(pos, cf)
            for book_side in ("bid", "ask"):
                p, c = pos, cf
                fills = [(o.side, o.price, o.remaining) for o in self.resting.values() if o.ticker == t]
                for s, px, n in fills + extra:
                    if s == book_side:
                        p, c = (p + n, c - px * n) if s == "bid" else (p - n, c + px * n)
                worst = max(worst, _worst_case_loss(p, c))
            return worst

        new = [(side, price, count)]
        this_market = market_loss(ticker, new)
        event = self.markets[ticker]["event_ticker"]
        others = sum(
            market_loss(t, [])
            for t in set(self.pos) | {o.ticker for o in self.resting.values()}
            if t != ticker and self.markets[t]["event_ticker"] == event
        )
        return this_market, this_market + others

    def _apply_fill(self, order: Order, price: float, n: float, fee: float) -> None:
        t = order.ticker
        signed = n if order.side == "bid" else -n
        flow = -price * n if order.side == "bid" else price * n
        self.pos[t] = self.pos.get(t, 0.0) + signed
        self.cash_flow[t] = self.cash_flow.get(t, 0.0) + flow - fee
        self.cash += flow - fee
        order.remaining -= n
        self.fills.append(Fill(self.now, t, order.side, price, n, fee, order.kind))

    # --- orders -----------------------------------------------------------

    def place(self, ticker: str, side: str, price: float, count: float, kind: str) -> Order | None:
        if side not in ("bid", "ask") or not 0 < price < 1 or count <= 0:
            raise ValueError(f"bad order {side} {count}@{price}")
        price = round(price, 4)
        if kind == "taker":
            last = self.last_price.get(ticker)
            if last is None:
                return None
            slip = self.config.taker_slippage
            price = min(0.99, last + slip) if side == "bid" else max(0.01, last - slip)
            count = min(count, self.last_size.get(ticker, 0.0))
            if count <= 0:
                return None
        market_loss, event_loss = self._exposure_after(ticker, side, price, count)
        if not self.risk.allows(market_loss, event_loss):
            return None
        order = Order(next(self._ids), ticker, side, price, count, kind, self.now)
        if kind == "taker":
            self._apply_fill(order, price, count, taker_fee(price, count))
        else:
            self.resting[order.id] = order
        return order

    def _match_resting(self, trade) -> None:
        q = self.config.queue_fill_fraction
        for order in [o for o in self.resting.values() if o.ticker == trade.ticker]:
            if order.side == "bid" and trade.taker_side == "no" and trade.price <= order.price:
                through = trade.price < order.price
            elif order.side == "ask" and trade.taker_side == "yes" and trade.price >= order.price:
                through = trade.price > order.price
            else:
                continue
            n = min(order.remaining, trade.count if through else trade.count * q)
            n = round(n, 2)
            if n <= 0:
                continue
            fee_type = self.markets[order.ticker].get("fee_type", "quadratic")
            self._apply_fill(order, order.price, n, maker_fee(order.price, n, fee_type))
            if order.remaining <= 1e-9:
                del self.resting[order.id]

    def _settle(self, ticker: str, strategy: Strategy, ctx: Context) -> None:
        result = self.markets[ticker]["result"]
        pos = self.pos.pop(ticker, 0.0)
        cf = self.cash_flow.pop(ticker, 0.0)
        payout = pos if result == "yes" else 0.0
        self.cash += payout
        for oid in [i for i, o in self.resting.items() if o.ticker == ticker]:
            del self.resting[oid]
        if pos or cf:
            m = self.markets[ticker]
            self.settlements.append(
                {"ts": self.now, "ticker": ticker, "event_ticker": m["event_ticker"], "result": result,
                 "position": pos, "pnl": cf + payout}
            )
        strategy.on_settle(ctx, ticker, result)

    # --- main loop --------------------------------------------------------

    def run(self, strategy: Strategy) -> "BacktestResult":
        ctx = Context(self)
        closes = sorted((m["close_time"], t) for t, m in self.markets.items())
        ci = 0
        for trade in self.trades.itertuples(index=False):
            while ci < len(closes) and closes[ci][0] < trade.ts:
                self.now = closes[ci][0]
                self._settle(closes[ci][1], strategy, ctx)
                ci += 1
            if trade.ts > self.markets[trade.ticker]["close_time"]:
                continue
            self.now = trade.ts
            self._match_resting(trade)
            self.last_price[trade.ticker] = trade.price
            self.last_size[trade.ticker] = trade.count
            equity = self.equity()
            self.risk.update(self.now, equity)
            if not self.risk.trading_allowed:
                self.resting.clear()
            else:
                strategy.on_trade(ctx, trade)
            self.equity_curve.append((self.now, equity))
        for close_time, ticker in closes[ci:]:
            self.now = close_time
            self._settle(ticker, strategy, ctx)
        self.equity_curve.append((self.now, self.equity()))
        return BacktestResult(self)


class BacktestResult:
    def __init__(self, bt: Backtester):
        self.starting_cash = bt.config.starting_cash
        self.final_equity = bt.equity()
        self.fills = pd.DataFrame([f.__dict__ for f in bt.fills])
        self.settlements = pd.DataFrame(bt.settlements)
        curve = pd.DataFrame(bt.equity_curve, columns=["ts", "equity"])
        self.equity = curve.drop_duplicates("ts", keep="last").set_index("ts")["equity"]
        self.risk_events = bt.risk.events

    def summary(self) -> dict:
        eq = self.equity
        drawdown = (eq / eq.cummax() - 1).min() if len(eq) else 0.0
        s = self.settlements
        return {
            "starting_cash": self.starting_cash,
            "final_equity": round(self.final_equity, 2),
            "return_pct": round(100 * (self.final_equity / self.starting_cash - 1), 2),
            "max_drawdown_pct": round(100 * float(drawdown), 2),
            "fills": len(self.fills),
            "contracts": float(self.fills["count"].sum()) if len(self.fills) else 0.0,
            "fees": round(float(self.fills["fee"].sum()), 2) if len(self.fills) else 0.0,
            "markets_traded": len(s),
            "market_win_rate": round(float((s["pnl"] > 0).mean()), 3) if len(s) else None,
            "risk_events": len(self.risk_events),
        }

    def by_price_band(self, bands=(0, 0.05, 0.10, 0.20, 0.50, 0.80, 0.90, 0.95, 1.0)) -> pd.DataFrame:
        """Fills grouped by the YES price they traded at."""
        if self.fills.empty:
            return pd.DataFrame()
        f = self.fills.assign(band=pd.cut(self.fills["price"], bands))
        return f.groupby("band", observed=True).agg(fills=("count", "size"), contracts=("count", "sum"), fees=("fee", "sum"))
