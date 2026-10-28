"""Download settled markets and their trades for a series into Parquet.

    python -m kalshi_bot.collector KXHIGHNY KXHIGHCHI --out data --since 2025-01-01

Writes data/<series>/markets.parquet and data/<series>/trades.parquet, pulling from
both the live and historical endpoints so markets on either side of the cutoff are covered.
"""

import argparse
from pathlib import Path

import pandas as pd

from .client import KalshiClient
from .data import normalize_markets, normalize_trades, save


def collect_series(
    client: KalshiClient, series_ticker: str, out: Path, since: pd.Timestamp | None, sample: int | None = None
) -> None:
    fee_type = client.get_series(series_ticker).get("fee_type", "quadratic")
    cutoff = pd.Timestamp(client.historical_cutoff()["trades_created_ts"])

    records = {}
    for historical in (True, False):
        params = {"series_ticker": series_ticker}
        if not historical:
            params["status"] = "settled"
        for m in client.markets(historical=historical, **params):
            records[m["ticker"]] = m
    markets = normalize_markets(list(records.values()), series_ticker, fee_type)
    markets = markets[markets["result"].isin(["yes", "no"])]
    if since is not None:
        markets = markets[markets["close_time"] >= since]
    if sample and len(markets) > sample:
        # Busy series (crypto, sports) have far too many trades to pull every market,
        # so take whole events spread evenly over the period to keep ladders intact.
        events = markets.sort_values("close_time")["event_ticker"].drop_duplicates().tolist()
        per_event = len(markets) / len(events)
        n_events = max(1, int(sample / per_event))
        step = max(1, len(events) // n_events)
        markets = markets[markets["event_ticker"].isin(events[::step][:n_events])]
    print(f"{series_ticker}: {len(markets)} settled markets")

    trade_records = []
    for i, ticker in enumerate(markets["ticker"], 1):
        seen = set()
        close_time = markets.loc[markets["ticker"] == ticker, "close_time"].iloc[0]
        open_time = markets.loc[markets["ticker"] == ticker, "open_time"].iloc[0]
        # Trades before the cutoff live only on /historical, later ones only on the live endpoint.
        sources = [h for h, needed in ((True, open_time < cutoff), (False, close_time >= cutoff)) if needed]
        for historical in sources:
            for t in client.trades(historical=historical, ticker=ticker):
                if t["trade_id"] not in seen:
                    seen.add(t["trade_id"])
                    trade_records.append(t)
        if i % 50 == 0:
            print(f"  {i}/{len(markets)} markets, {len(trade_records)} trades")
    trades = normalize_trades(trade_records)

    save(markets, out / series_ticker / "markets.parquet")
    save(trades, out / series_ticker / "trades.parquet")
    print(f"{series_ticker}: saved {len(trades)} trades")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("series", nargs="+")
    parser.add_argument("--out", type=Path, default=Path("data"))
    parser.add_argument("--since", type=pd.Timestamp, default=None)
    parser.add_argument("--sample", type=int, default=None, help="at most about this many markets per series")
    args = parser.parse_args()
    since = args.since.tz_localize("UTC") if args.since is not None else None
    client = KalshiClient(env="prod")  # read-only public data; no orders are sent
    for series in args.series:
        collect_series(client, series, args.out, since, args.sample)


if __name__ == "__main__":
    main()
