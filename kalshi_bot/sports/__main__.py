"""Sports picks: model every upcoming game and list plays priced below the model.

    python -m kalshi_bot.sports picks             # high-EV plays on Kalshi and Polymarket US
    python -m kalshi_bot.sports picks --all       # every priced contract, not just flagged ones
    python -m kalshi_bot.sports picks --csv out.csv
    python -m kalshi_bot.sports backtest          # how accurate the model's percentages have been
    python -m kalshi_bot.sports ratings nfl       # current team ratings

Advice only: nothing here places an order. Settings come from environment variables;
see kalshi_bot/sports/config.py.
"""

import argparse
import csv
import os
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import LEAGUES, SportsConfig
from .model import PARAMS, Elo, backtest
from .plays import Play, model_games, plays_for
from .results import ResultsClient
from .teams import match_pair
from .venues import build_venues

EASTERN = ZoneInfo("America/New_York")


def pct(x: float | None) -> str:
    return "  -  " if x is None else f"{x * 100:4.1f}%"


def table(rows: list[list[str]], header: list[str]) -> str:
    widths = [max(len(str(r[i])) for r in [header] + rows) for i in range(len(header))]
    line = lambda r: "  ".join(str(c).ljust(w) for c, w in zip(r, widths)).rstrip()
    return "\n".join([line(header), line(["-" * w for w in widths])] + [line(r) for r in rows])


def sharp_probabilities(league, games, cfg) -> dict[tuple, float]:
    """Devigged sharp-book probabilities keyed like the model's games, if ODDS_API_KEY is set."""
    if not os.environ.get("ODDS_API_KEY"):
        return {}
    from .odds import OddsClient

    out = {}
    for s in OddsClient().fair_values(league.key, league.odds_api_sport, cfg.sharp_books):
        for g in games:
            mapping = match_pair((s.home, s.away), (g.home, g.away))
            if mapping and abs(s.commence - g.commence) < timedelta(hours=6):
                for team, p in s.fair.items():
                    out[(g.home, g.away, mapping[team])] = p
    return out


def collect_plays(cfg: SportsConfig, results: ResultsClient) -> tuple[list[Play], list[str]]:
    venues = build_venues(cfg.venues)
    plays, notes = [], []
    for key in cfg.leagues:
        league = LEAGUES[key]
        upcoming = results.upcoming(key, cfg.days_ahead)
        if not upcoming:
            notes.append(f"{key}: no games in the next {cfg.days_ahead} days")
            continue
        elo = Elo(PARAMS[key]).fit(results.history(key, cfg.history_years))
        games = model_games(upcoming, elo)
        matched = []
        for name, venue in venues.items():
            try:
                found = venue.contracts(league, games)
            except Exception as e:  # one venue being down shouldn't hide the other's prices
                notes.append(f"{key} {name}: could not read prices ({e})")
                continue
            matched.extend(found)
            notes.append(f"{key} {name}: priced {len({m.game.id for m in found})} of {len(games)} games")
        try:
            sharp = sharp_probabilities(league, games, cfg)
        except Exception as e:
            sharp = {}
            notes.append(f"{key}: sharp lines unavailable ({e})")
        plays.extend(plays_for(matched, elo, cfg, sharp))
    return sorted(plays, key=lambda p: p.ev, reverse=True), notes


def show_plays(plays: list[Play], cfg: SportsConfig, show_all: bool) -> None:
    shown = plays if show_all else [p for p in plays if p.flagged(cfg)]
    has_sharp = any(p.sharp_p is not None for p in shown)
    header = ["Bet", "Game", "Start (ET)", "Venue", "Price", "Model", "Edge", "EV", "Stake"]
    if has_sharp:
        header.insert(6, "Sharp")
    rows = []
    for p in shown:
        start = datetime.fromisoformat(p.start).astimezone(EASTERN).strftime("%a %b %d %I:%M%p")
        row = [
            ("* " if p.flagged(cfg) else "  ") + p.team + (" (thin data)" if p.thin_data else ""),
            f"{p.league.upper()} {p.game}", start, p.venue, f"{p.price * 100:.0f}c", pct(p.model_p),
            f"{p.edge * 100:+.1f}pt", f"{p.ev * 100:+.1f}%", f"${p.stake(cfg):.2f}" if p.edge > 0 else "",
        ]
        if has_sharp:
            row.insert(6, pct(p.sharp_p))
        rows.append(row)
    title = "All priced contracts" if show_all else f"Plays with EV of at least {cfg.min_ev:.0%} after fees"
    print(f"\n{title} (* = flagged)\n")
    print(table(rows, header) if rows else "None right now.")
    print(
        "\nPrice is the ask; Model is the model's win probability; Edge = Model - (Price + fee);"
        "\nEV is expected profit per $1 staked; Stake is a quarter-Kelly suggestion on a "
        f"${cfg.bankroll:,.0f} bankroll."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    picks = sub.add_parser("picks")
    picks.add_argument("--all", action="store_true", help="show every priced contract")
    picks.add_argument("--csv", help="also write every play to this CSV file")
    bt = sub.add_parser("backtest")
    bt.add_argument("--test-years", type=float, default=1.0, help="score the most recent N years")
    rt = sub.add_parser("ratings")
    rt.add_argument("league", choices=sorted(LEAGUES))
    rt.add_argument("--top", type=int, default=40)
    args = parser.parse_args()

    cfg = SportsConfig.from_env()
    results = ResultsClient()

    if args.cmd == "picks":
        plays, notes = collect_plays(cfg, results)
        print("\n".join(notes))
        show_plays(plays, cfg, args.all)
        if args.csv:
            with open(args.csv, "w", newline="") as f:
                fields = list(asdict(plays[0])) + ["cost", "edge", "ev", "stake", "flagged"] if plays else ["none"]
                w = csv.DictWriter(f, fields)
                w.writeheader()
                for p in plays:
                    w.writerow({**asdict(p), "cost": p.cost, "edge": p.edge, "ev": p.ev,
                                "stake": p.stake(cfg), "flagged": p.flagged(cfg)})
            print(f"\nWrote {len(plays)} rows to {args.csv}")
    elif args.cmd == "backtest":
        test_from = datetime.now(timezone.utc) - timedelta(days=int(365 * args.test_years))
        for key in cfg.leagues:
            games = results.history(key, cfg.history_years + args.test_years)
            r = backtest(games, PARAMS[key], test_from)
            if not r["games"]:
                print(f"\n{key.upper()}: no games to score")
                continue
            print(
                f"\n{key.upper()}: {r['games']} games since {test_from:%Y-%m-%d}. Picked the winner "
                f"{r['accuracy']:.1%} of the time. Log loss {r['log_loss']:.4f} "
                f"(home-team baseline {r['baseline_log_loss']:.4f}), Brier {r['brier']:.4f}."
            )
            rows = [[band, v["games"], pct(v["predicted"]), pct(v["actual"])] for band, v in r["calibration"].items()]
            print(table(rows, ["Favourite at", "Games", "Predicted", "Actually won"]))
    else:
        elo = Elo(PARAMS[args.league]).fit(results.history(args.league, cfg.history_years))
        ranked = sorted(elo.ratings.items(), key=lambda kv: kv[1], reverse=True)[: args.top]
        print(table([[i + 1, t, f"{r:.0f}", elo.experience(t)] for i, (t, r) in enumerate(ranked)],
                    ["#", "Team", "Rating", "Games"]))


if __name__ == "__main__":
    main()
