"""Scan open bracket events for ladders whose top-of-book prices don't add up after fees.

    python scripts/scan_brackets.py KXHIGHNY KXHIGHCHI

Read-only: uses public market data and places no orders. Prints, per event, the cost of
buying every YES at the ask and every NO at 1 - bid, so near-misses are visible too.
"""

import argparse
from collections import defaultdict

from kalshi_bot.client import KalshiClient
from kalshi_bot.fees import taker_fee
from kalshi_bot.strategies.bracket import Quote, scan_event


def _price(m: dict, name: str) -> float | None:
    v = m.get(f"{name}_dollars")
    return float(v) if v not in (None, "") else None


def quotes_for(m: dict, fee_multiplier: float) -> Quote:
    bid, ask = _price(m, "yes_bid"), _price(m, "yes_ask")
    bid_size, ask_size = float(m.get("yes_bid_size_fp") or 0), float(m.get("yes_ask_size_fp") or 0)
    return Quote(
        m["ticker"],
        bid if bid and bid_size > 0 else None,
        ask if ask and ask_size > 0 else None,
        bid_size,
        ask_size,
        fee_multiplier,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("series", nargs="+")
    args = parser.parse_args()
    client = KalshiClient(env="prod")  # public data only
    found = 0
    for series in args.series:
        fee_multiplier = float(client.get_series(series).get("fee_multiplier", 1))
        events = defaultdict(list)
        for m in client.markets(series_ticker=series, status="open"):
            events[m["event_ticker"]].append(quotes_for(m, fee_multiplier))
        for event, quotes in sorted(events.items()):
            asks = [q.yes_ask for q in quotes]
            bids = [q.yes_bid for q in quotes]
            yes_cost = sum(a + taker_fee(a, 1, fee_multiplier) for a in asks) if None not in asks else None
            no_cost = sum(1 - b + taker_fee(1 - b, 1, fee_multiplier) for b in bids) if None not in bids else None
            fmt = lambda x: "  n/a" if x is None else f"{x:5.2f}"
            print(f"{event:24} n={len(quotes)}  all-YES cost {fmt(yes_cost)} (pays 1.00)  "
                  f"all-NO cost {fmt(no_cost)} (pays {len(quotes) - 1}.00)")
            for opp in scan_event(event, quotes):
                found += 1
                print(f"   ** {opp.direction}: {opp.contracts} contracts, cost {opp.cost}, profit {opp.profit}")
    print(f"\n{found} locked-in opportunities after taker fees")


if __name__ == "__main__":
    main()
