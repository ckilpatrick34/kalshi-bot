"""Paper-trade the NBA longshot strategy on live Kalshi trades. No account or key needed.

    python scripts/paper_trade.py --out paper

Polls public trades for open markets in the series, runs the same strategy and fill model
as the backtest, and settles positions when Kalshi settles the market. Every fill,
settlement and equity snapshot is appended to <out>/log.jsonl, so a forward record builds
up that can be compared with the backtest before any real order is sent.
"""

import argparse
import json
import time
from pathlib import Path

import pandas as pd

from kalshi_bot.backtest import Backtester, BacktestConfig, Context
from kalshi_bot.client import KalshiClient
from kalshi_bot.data import normalize_trades
from kalshi_bot.strategies import LongshotMaker

STRATEGY = dict(max_cheap=0.05, edge=0.01, max_hours_to_close=6, sides=("yes", "no"), clock="expected_expiration_time")


def log(out: Path, kind: str, **fields) -> None:
    row = {"logged": pd.Timestamp.now(tz="UTC").isoformat(), "kind": kind, **fields}
    with open(out / "log.jsonl", "a") as f:
        f.write(json.dumps(row, default=str) + "\n")
    print(row)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--series", default="KXNBAGAME")
    parser.add_argument("--out", type=Path, default=Path("paper"))
    parser.add_argument("--cash", type=float, default=500.0)
    parser.add_argument("--poll", type=float, default=20.0, help="seconds between polls")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    client = KalshiClient(env="prod")  # public data only; this script never sends orders
    fee_type = client.get_series(args.series).get("fee_type", "quadratic")
    engine = Backtester(pd.DataFrame(columns=["ticker"]), pd.DataFrame(columns=["ts", "ticker"]),
                        BacktestConfig(starting_cash=args.cash))
    strategy, ctx = LongshotMaker(**STRATEGY), Context(engine)
    last_seen: dict[str, pd.Timestamp] = {}
    seen_ids: set[str] = set()
    n_fills = n_settled = 0
    log(args.out, "start", strategy=STRATEGY, cash=args.cash, series=args.series)

    while True:
        now = pd.Timestamp.now(tz="UTC")
        open_markets = {m["ticker"]: m for m in client.markets(series_ticker=args.series, status="open")}
        for t, m in open_markets.items():
            if t not in engine.markets:
                engine.add_market({
                    "ticker": t, "event_ticker": m["event_ticker"], "series_ticker": args.series,
                    "fee_type": fee_type, "result": None,
                    "close_time": pd.Timestamp(m["close_time"]),
                    "expected_expiration_time": pd.Timestamp(m["expected_expiration_time"]),
                })

        # Only markets inside the trading window need trades; that keeps polling light.
        active = [t for t, m in engine.markets.items() if t in open_markets
                  and (m["expected_expiration_time"] - now).total_seconds() / 3600 <= STRATEGY["max_hours_to_close"] + 0.5]
        records = []
        for t in active:
            params = {"ticker": t}
            if t in last_seen:
                params["min_ts"] = int(last_seen[t].timestamp())
            records += [r for r in client.trades(**params) if r["trade_id"] not in seen_ids]
        trades = normalize_trades(records)
        seen_ids.update(r["trade_id"] for r in records)
        for trade in trades.itertuples(index=False):
            engine.process_trade(trade, strategy, ctx)
            last_seen[trade.ticker] = trade.ts
        for f in engine.fills[n_fills:]:
            log(args.out, "fill", **f.__dict__)
        n_fills = len(engine.fills)

        # Settle markets that have left the open list once Kalshi publishes a result.
        held = set(engine.pos) | {o.ticker for o in engine.resting.values()}
        for t in [t for t in list(engine.markets) if t not in open_markets]:
            if t in held:
                result = client._request("GET", f"/markets/{t}")["market"].get("result")
                if result not in ("yes", "no"):
                    continue
                engine.markets[t]["result"] = result
                engine.settle(t, pd.Timestamp.now(tz="UTC"), strategy, ctx)
            del engine.markets[t]
        for s in engine.settlements[n_settled:]:
            log(args.out, "settle", **s)
        n_settled = len(engine.settlements)

        log(args.out, "equity", equity=round(engine.equity(), 2), cash=round(engine.cash, 2),
            open_positions=len(engine.pos), resting=len(engine.resting), active_markets=len(active),
            halted=engine.risk.halted)
        time.sleep(args.poll)


if __name__ == "__main__":
    main()
