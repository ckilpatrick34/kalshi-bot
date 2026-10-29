"""Command line.

    python -m marketlines poll --sports ncaaf,ncaab      one snapshot per sport, then exit (for cron)
    python -m marketlines run --sports ncaaf,ncaab       keep polling on an adaptive schedule
    python -m marketlines settle                         capture closes and score CLV
    python -m marketlines signals --hours 24             recent signals
    python -m marketlines ledger --days 90               rolling CLV by sport and signal type
    python -m marketlines backfill --sport ncaaf --start 2026-08-23 --step 3h [--dry-run]
    python -m marketlines splits FILE.csv                load betting splits for true RLM
"""

import argparse
import csv
import logging
import statistics
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .engine import backfill, backfill_cost, live_cost, next_delay, poll, settle
from .feed import OddsApi
from .prices import decimal_to_american
from .sports import SPORTS
from .store import Store, parse_ts


def parse_step(text: str) -> timedelta:
    unit = text[-1]
    value = float(text[:-1])
    return {"m": timedelta(minutes=value), "h": timedelta(hours=value), "d": timedelta(days=value)}[unit]


def sports_arg(text: str):
    return [SPORTS[name.strip()] for name in text.split(",") if name.strip()]


def fmt_line(market: str, side: str, line, price: float) -> str:
    if market == "spreads" and line is not None:
        number = line if side == "home" else -line
        return f"{side} {number:+g} ({decimal_to_american(price):+d})"
    if market == "totals" and line is not None:
        return f"{side} {line:g} ({decimal_to_american(price):+d})"
    return f"{side} ML ({decimal_to_american(price):+d})"


def cmd_poll(args, store):
    api = OddsApi()
    for sport in args.sports:
        for s in poll(store, api, sport):
            print(f"{s.sport} {s.kind:<10} {s.market:<7} {fmt_line(s.market, s.side_name, s.line, s.price):<26} "
                  f"@ {s.book:<14} fair {s.fair:.3f} EV {s.ev:+.1%} score {s.score:.0f}")
    print(f"credits remaining: {api.credits_remaining}")


def cmd_run(args, store):
    api = OddsApi()
    due = {sport.name: datetime.now(timezone.utc) for sport in args.sports}
    while True:
        now = datetime.now(timezone.utc)
        for sport in args.sports:
            if due[sport.name] > now:
                continue
            try:
                poll(store, api, sport, now)
            except Exception:
                logging.exception("poll failed for %s", sport.name)
                due[sport.name] = now + timedelta(minutes=5)
                continue
            commences = [e[2] for e in store.events(sport.name)]
            due[sport.name] = now + next_delay(now, commences, api.credits_remaining, live_cost(sport))
            logging.info("%s next poll %s (credits %s)", sport.name, due[sport.name].isoformat(), api.credits_remaining)
        time.sleep(max(5.0, (min(due.values()) - datetime.now(timezone.utc)).total_seconds()))


def cmd_settle(args, store):
    closes, scored = settle(store, datetime.now(timezone.utc))
    print(f"captured {closes} closes, scored CLV on {scored} signals")


def cmd_signals(args, store):
    since = datetime.now(timezone.utc) - timedelta(hours=args.hours)
    for fired, sport, away, home, market, kind, side, book, line, price, fair, score, clv in store.recent_signals(since, args.min_score):
        clv_text = f"CLV {clv:+.1%}" if clv is not None else "open"
        print(f"{parse_ts(fired):%m-%d %H:%M} {sport:<8} {away} @ {home}: {kind:<10} "
              f"{fmt_line(market, side, line, price)} @ {book}, fair {fair:.3f}, score {score:.0f}, {clv_text}")


def cmd_ledger(args, store):
    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    groups = defaultdict(list)
    for sport, kind, market, clv_ev, clv_move, clv_points, _score in store.ledger(since):
        groups[(sport, kind)].append((clv_ev, clv_move, clv_points))
    print(f"CLV ledger, last {args.days} days (signals with a captured close)")
    print(f"{'sport':<9}{'signal':<12}{'n':>5}{'avg CLV':>9}{'beat close':>12}{'avg move':>10}{'avg pts':>9}  status")
    for (sport, kind), rows in sorted(groups.items()):
        ev = [r[0] for r in rows]
        pts = [r[2] for r in rows if r[2] is not None]
        avg = statistics.mean(ev)
        status = "retire" if len(rows) >= 30 and avg < 0 else "ok" if len(rows) >= 30 else "small sample"
        print(f"{sport:<9}{kind:<12}{len(rows):>5}{avg:>+9.2%}{sum(e > 0 for e in ev) / len(ev):>12.0%}"
              f"{statistics.mean(r[1] for r in rows):>+10.2%}{(statistics.mean(pts) if pts else 0):>+9.2f}  {status}")


def cmd_backfill(args, store):
    sport = SPORTS[args.sport]
    start = parse_ts(args.start)
    end = parse_ts(args.end) if args.end else datetime.now(timezone.utc)
    step = parse_step(args.step)
    cost = backfill_cost(sport, start, end, step)
    print(f"{sport.name}: {start:%Y-%m-%d} to {end:%Y-%m-%d} every {args.step} = about {cost:,} credits")
    if args.dry_run:
        return
    used = backfill(store, OddsApi(), sport, start, end, step)
    print(f"done, used {used:,} credits")


def cmd_splits(args, store):
    """CSV columns: event_id, market, ts, ticket_share_a, money_share_a, source (shares 0-1, side A = home/over)."""
    with open(args.file, newline="") as f:
        rows = [(r["event_id"], r["market"], parse_ts(r["ts"]), float(r["ticket_share_a"]),
                 float(r["money_share_a"]) if r.get("money_share_a") else None, r.get("source") or "csv")
                for r in csv.DictReader(f)]
    store.insert_splits(rows)
    store.commit()
    print(f"loaded {len(rows)} split rows")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="marketlines", description="College line-movement signals and CLV ledger")
    parser.add_argument("--db", help="SQLite path or postgres:// URL (default: MARKETLINES_DB or marketlines.db)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("poll")
    p.add_argument("--sports", type=sports_arg, default=sports_arg("ncaaf,ncaab"))
    p.set_defaults(func=cmd_poll)
    p = sub.add_parser("run")
    p.add_argument("--sports", type=sports_arg, default=sports_arg("ncaaf,ncaab"))
    p.set_defaults(func=cmd_run)
    sub.add_parser("settle").set_defaults(func=cmd_settle)
    p = sub.add_parser("signals")
    p.add_argument("--hours", type=float, default=24)
    p.add_argument("--min-score", type=float, default=0)
    p.set_defaults(func=cmd_signals)
    p = sub.add_parser("ledger")
    p.add_argument("--days", type=int, default=90)
    p.set_defaults(func=cmd_ledger)
    p = sub.add_parser("backfill")
    p.add_argument("--sport", choices=sorted(SPORTS), required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end")
    p.add_argument("--step", default="3h", help="time between historical snapshots, e.g. 30m, 3h, 1d")
    p.add_argument("--dry-run", action="store_true", help="print the credit cost and stop")
    p.set_defaults(func=cmd_backfill)
    p = sub.add_parser("splits")
    p.add_argument("file")
    p.set_defaults(func=cmd_splits)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose or args.command == "run" else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")
    args.func(args, Store(args.db))


if __name__ == "__main__":
    main()
