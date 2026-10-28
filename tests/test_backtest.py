import pandas as pd

from kalshi_bot.backtest import Backtester, BacktestConfig, Strategy
from kalshi_bot.data import normalize_trades
from kalshi_bot.risk import RiskLimits
from kalshi_bot.strategies import LongshotMaker

T0 = pd.Timestamp("2026-01-01 12:00", tz="UTC")


def markets(*rows):
    return pd.DataFrame(
        [
            {"ticker": t, "event_ticker": e, "series_ticker": "S", "open_time": T0 - pd.Timedelta(days=1),
             "close_time": T0 + pd.Timedelta(hours=h), "result": r, "fee_type": "quadratic"}
            for t, e, h, r in rows
        ]
    )


def trades(*rows):
    return pd.DataFrame(
        [{"ts": T0 + pd.Timedelta(minutes=m), "ticker": t, "price": p, "count": c, "taker_side": s}
         for m, t, p, c, s in rows]
    )


class OneShot(Strategy):
    """Places one order on the first trade it sees."""

    def __init__(self, side, price, count, kind="maker"):
        self.args, self.done = (side, price, count, kind), False

    def on_trade(self, ctx, trade):
        if not self.done:
            self.done = True
            ctx.place(trade.ticker, *self.args)


def test_resting_ask_fills_at_price_with_queue_and_settles_no():
    m = markets(("A", "E", 5, "no"))
    t = trades((0, "A", 0.05, 10, "yes"), (1, "A", 0.06, 20, "yes"))
    res = Backtester(m, t).run(OneShot("ask", 0.06, 10))
    assert res.fills["count"].sum() == 10  # half of the 20 traded at our price
    assert round(res.final_equity, 2) == 500.60  # sold YES at 6c, settled NO, no maker fee


def test_ask_does_not_fill_when_taker_is_selling():
    m = markets(("A", "E", 5, "no"))
    t = trades((0, "A", 0.05, 10, "yes"), (1, "A", 0.07, 20, "no"))
    assert Backtester(m, t).run(OneShot("ask", 0.06, 10)).fills.empty


def test_trade_through_price_fills_fully():
    m = markets(("A", "E", 5, "yes"))
    t = trades((0, "A", 0.50, 10, "yes"), (1, "A", 0.45, 10, "no"))
    res = Backtester(m, t).run(OneShot("bid", 0.48, 10))
    assert res.fills["count"].sum() == 10
    assert round(res.final_equity, 2) == 505.20


def test_taker_pays_fee_and_slippage():
    m = markets(("A", "E", 5, "yes"))
    t = trades((0, "A", 0.50, 100, "yes"))
    cfg = BacktestConfig(limits=RiskLimits(max_market_loss_pct=0.2, max_event_loss_pct=0.2))
    res = Backtester(m, t, cfg).run(OneShot("bid", 0.50, 100, "taker"))
    fill = res.fills.iloc[0]
    assert fill.price == 0.51 and fill.fee == 1.75
    assert round(res.final_equity, 2) == round(500 + 0.49 * 100 - 1.75, 2)


def test_market_loss_cap_rejects_oversized_order():
    # 5% of $500 = $25 max loss per market; 100 contracts at 50c risks $50.
    m = markets(("A", "E", 5, "yes"))
    t = trades((0, "A", 0.50, 10, "yes"))
    bt = Backtester(m, t)
    res = bt.run(OneShot("bid", 0.50, 100))
    assert res.fills.empty and not bt.resting


def test_event_loss_cap_counts_sibling_markets():
    limits = RiskLimits(max_market_loss_pct=0.05, max_event_loss_pct=0.06)
    m = markets(("A", "E", 5, "no"), ("B", "E", 5, "no"))
    t = trades((0, "A", 0.50, 10, "yes"), (1, "B", 0.50, 10, "yes"))

    placed = {}

    class Both(Strategy):
        def on_trade(self, ctx, trade):
            placed[trade.ticker] = ctx.place(trade.ticker, "bid", 0.40, 50)  # $20 worst case each

    Backtester(m, t, BacktestConfig(limits=limits)).run(Both())
    # A's $20 fits under the $30 event cap; B would take the event to $40.
    assert placed["A"] is not None and placed["B"] is None


def test_drawdown_kill_switch_halts_trading():
    cfg = BacktestConfig(starting_cash=100, limits=RiskLimits(max_market_loss_pct=1, max_event_loss_pct=1))
    m = markets(("A", "E1", 1, "no"), ("B", "E2", 5, "yes"))
    t = trades(
        (0, "A", 0.90, 100, "yes"),
        (30, "A", 0.60, 100, "no"),  # marked loss of $30 on $100
        (90, "B", 0.50, 100, "yes"),
    )
    res = Backtester(m, t, cfg).run(OneShot("bid", 0.90, 100, "taker"))
    assert "drawdown_kill_switch" in [e[1] for e in res.risk_events]


def test_longshot_strategy_runs_end_to_end():
    m = markets(("A", "E", 24, "no"), ("B", "E", 24, "yes"))
    t = trades(
        (0, "A", 0.08, 50, "yes"), (5, "A", 0.08, 50, "yes"), (10, "A", 0.09, 50, "yes"),
        (0, "B", 0.93, 50, "no"), (5, "B", 0.92, 50, "no"),
    )
    s = Backtester(m, t).run(LongshotMaker()).summary()
    assert s["fills"] > 0 and s["final_equity"] > 500


def test_normalize_trades_reads_dollar_strings_and_skips_block_trades():
    df = normalize_trades([
        {"trade_id": "1", "ticker": "A", "count_fp": "10.00", "yes_price_dollars": "0.5600",
         "taker_outcome_side": "yes", "created_time": "2026-01-01T12:00:00Z", "is_block_trade": False},
        {"trade_id": "2", "ticker": "A", "count_fp": "5.00", "yes_price_dollars": "0.5000",
         "taker_outcome_side": "no", "created_time": "2026-01-01T12:01:00Z", "is_block_trade": True},
    ])
    assert len(df) == 1 and df.loc[0, "price"] == 0.56 and df.loc[0, "count"] == 10
