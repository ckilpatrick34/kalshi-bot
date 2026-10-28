"""Sports engine command line.

    python -m kalshi_bot.sports scan            # read-only: list contracts priced better than sharp fair value
    python -m kalshi_bot.sports run --once      # one trading cycle (paper unless SPORTS_MODE=live)
    python -m kalshi_bot.sports run --every 300 # keep cycling every 5 minutes
    python -m kalshi_bot.sports settle          # grade finished games from The Odds API scores
    python -m kalshi_bot.sports report          # P&L and closing-line value from the ledger

Settings come from environment variables; see kalshi_bot/sports/config.py.
"""

import argparse
import json
import logging
import os
import time

from .config import SportsConfig
from .engine import Engine
from .ledger import Ledger
from .odds import OddsClient
from .venues import build_venues


def _check_live(cfg: SportsConfig) -> None:
    if cfg.mode != "live":
        return
    flags = {"kalshi": "KALSHI_ALLOW_LIVE", "polymarket": "POLYMARKET_US_ALLOW_LIVE"}
    missing = [flags[v] for v in cfg.venues if os.environ.get(flags[v]) != "1"]
    if "kalshi" in cfg.venues and os.environ.get("KALSHI_ENV", "demo") != "prod":
        print("Note: KALSHI_ENV is not 'prod', so Kalshi orders go to the demo exchange.")
    if missing:
        raise SystemExit(f"SPORTS_MODE=live also needs {', '.join(missing)}=1. Refusing to trade.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan")
    run = sub.add_parser("run")
    run.add_argument("--once", action="store_true")
    run.add_argument("--every", type=float, default=300, help="seconds between cycles")
    sub.add_parser("settle")
    rep = sub.add_parser("report")
    rep.add_argument("--mode", choices=["paper", "live"])
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")

    cfg = SportsConfig.from_env()
    ledger = Ledger(cfg.ledger_path)

    if args.cmd == "report":
        print(json.dumps(ledger.report(args.mode), indent=2, default=lambda x: round(x, 4)))
        return

    odds = OddsClient()
    engine = Engine(cfg, odds, build_venues(cfg.venues), ledger)
    if args.cmd == "scan":
        events = engine.cycle(trade=False)
        print("\n".join(events) or "No contract beats sharp fair value by the minimum edge right now.")
    elif args.cmd == "settle":
        print("\n".join(engine.settle()) or "Nothing to settle.")
    else:
        _check_live(cfg)
        print(f"mode={cfg.mode} venues={','.join(cfg.venues)} leagues={','.join(cfg.leagues)} bankroll={cfg.bankroll}")
        while True:
            try:
                for line in engine.cycle(trade=True):
                    print(line)
                print(f"-- cycle done, odds credits left: {odds.remaining_credits}")
            except Exception as e:  # a feed outage shouldn't kill the loop; the next cycle retries
                if args.once:
                    raise
                logging.exception("cycle failed: %s", e)
            if args.once:
                return
            time.sleep(args.every)


if __name__ == "__main__":
    main()
