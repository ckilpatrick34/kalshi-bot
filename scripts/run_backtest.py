"""Backtest the longshot maker strategy on collected data with a time split.

    python scripts/run_backtest.py --data data --split 2026-07-01

Parameters are compared on markets that closed before --split. Only the single best
setting is then run once on markets that closed after it, which is the number to trust.
"""

import argparse
import itertools
from pathlib import Path

import pandas as pd

from kalshi_bot.backtest import Backtester, BacktestConfig
from kalshi_bot.data import load_dataset
from kalshi_bot.strategies import LongshotMaker

GRID = {
    "max_cheap": [0.05, 0.10, 0.15],
    "edge": [0.0, 0.01],
    "max_hours_to_close": [6, 24, 72],
    "sides": [("yes",), ("no",), ("yes", "no")],
}


def split(markets: pd.DataFrame, trades: pd.DataFrame, cutoff: pd.Timestamp, before: bool):
    m = markets[(markets["close_time"] < cutoff) == before]
    return m, trades[trades["ticker"].isin(m["ticker"])]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--split", type=pd.Timestamp, required=True)
    parser.add_argument("--cash", type=float, default=500.0)
    parser.add_argument("--series", nargs="*", help="only these series (default: everything under --data)")
    args = parser.parse_args()

    markets, trades = load_dataset(args.data, args.series)
    cutoff = args.split.tz_localize("UTC")
    train = split(markets, trades, cutoff, before=True)
    test = split(markets, trades, cutoff, before=False)
    print(f"train: {len(train[0])} markets, {len(train[1])} trades | test: {len(test[0])} markets, {len(test[1])} trades")

    rows = []
    for values in itertools.product(*GRID.values()):
        params = dict(zip(GRID, values))
        result = Backtester(*train, BacktestConfig(starting_cash=args.cash)).run(LongshotMaker(**params))
        rows.append({**params, **result.summary()})
    table = pd.DataFrame(rows).sort_values("return_pct", ascending=False)
    print(f"\nTried {len(table)} settings on the training period. Top 10:")
    print(table.head(10).to_string(index=False))

    best = {k: table.iloc[0][k] for k in GRID}
    result = Backtester(*test, BacktestConfig(starting_cash=args.cash)).run(LongshotMaker(**best))
    print(f"\nOut-of-sample with {best}:")
    for k, v in result.summary().items():
        print(f"  {k}: {v}")
    print(result.by_price_band().to_string())


if __name__ == "__main__":
    main()
