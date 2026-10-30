"""College football game-day board: Kalshi prices against the sportsbook line. Never sends orders.

    python scripts/gameday.py board                    # this Saturday's flagged plays
    python scripts/gameday.py board --all              # every matched game, both teams
    python scripts/gameday.py board --paper            # also log flagged plays as paper bets
    python scripts/gameday.py board --date 2026-10-31 --min-ev 0.05 --max-bet 5
    python scripts/gameday.py settle                   # settle paper bets, print the record

Paper bets go to paper/gameday.jsonl. No account or key needed.
"""

import argparse
import csv
from dataclasses import asdict
from datetime import date
from pathlib import Path

from kalshi_bot.client import KalshiClient
from kalshi_bot.gameday import (
    SERIES, Settings, build_plays, fetch_book_games, kalshi_games, match_games, next_game_day, read_ledger,
    record, settle, size_plays,
)


def table(rows: list[list], header: list[str]) -> str:
    widths = [max(len(str(r[i])) for r in [header] + rows) for i in range(len(header))]
    line = lambda r: "  ".join(str(c).ljust(w) for c, w in zip(r, widths)).rstrip()
    return "\n".join([line(header), line(["-" * w for w in widths])] + [line(r) for r in rows])


def board(args, s: Settings) -> None:
    day = args.date or next_game_day()
    client = KalshiClient(env="prod")  # public market data only
    book = fetch_book_games(day)
    kalshi = kalshi_games(list(client.markets(series_ticker=SERIES, status="open")), day)
    pairs = list(match_games(kalshi, book))
    plays = size_plays(build_plays(pairs, s), s)
    no_line = sum(1 for g, _ in pairs if not g.fair)
    print(f"{day:%A %b %d}: {len(book)} games with a book listing, {len(kalshi)} Kalshi games, "
          f"{len(pairs)} matched ({no_line} without a moneyline yet).")

    shown = plays if args.all else [p for p in plays if p.flagged]
    rows = [[("* " if p.flagged else "  ") + p.team, p.game, p.start, p.line, f"{p.side.upper()} {p.ticker}",
             f"{p.ask * 100:.0f}c", f"{p.fair * 100:.1f}%", f"{p.ev * 100:+.1f}%", f"{p.limit_price * 100:.0f}c",
             f"{p.contracts} for ${p.stake:.2f}" if p.flagged else ", ".join(p.notes)] for p in shown]
    print(f"\n{'Every matched team' if args.all else f'Plays with EV of at least {s.min_ev:.0%} after the taker fee'}"
          " (* = flagged)\n")
    print(table(rows, ["Back", "Game", "Kickoff ET", "Book line", "Kalshi contract", "Ask", "Fair", "EV",
                       "Limit", "Paper size"]) if rows else "None right now.")
    print("\nFair = DraftKings moneyline with the margin removed. EV = Fair / (Ask + taker fee) - 1."
          "\nLimit = the highest resting bid that still clears the EV bar (no maker fee on this series)."
          f"\nSizes: quarter Kelly on ${s.bankroll:,.0f}, at most ${s.max_bet:,.0f} a play and "
          f"${s.day_cap:,.0f} in total, one side per game. Advice only: nothing was sent to Kalshi.")

    if args.csv:
        with open(args.csv, "w", newline="") as f:
            w = csv.DictWriter(f, list(asdict(plays[0])) if plays else ["none"])
            w.writeheader()
            w.writerows(asdict(p) for p in plays)
        print(f"\nWrote {len(plays)} rows to {args.csv}")
    if args.paper:
        n = record(args.ledger, plays)
        print(f"\nLogged {n} new paper bets to {args.ledger}")


def show_record(args) -> None:
    client = KalshiClient(env="prod")

    def result_of(ticker: str):
        return client._request("GET", f"/markets/{ticker}")["market"].get("result")

    bets = settle(args.ledger, result_of) if args.ledger.exists() else read_ledger(args.ledger)
    if not bets:
        print(f"No paper bets in {args.ledger}")
        return
    rows = [[b["placed"][:16], b["team"], b["game"], f"{b['side'].upper()} {b['ticker']}", f"{b['ask'] * 100:.0f}c",
             b["contracts"], f"${b['stake']:.2f}", b["status"], f"${b['pnl']:+.2f}" if "pnl" in b else ""] for b in bets]
    print(table(rows, ["Placed", "Team", "Game", "Contract", "Price", "Qty", "Stake", "Status", "P&L"]))
    done = [b for b in bets if "pnl" in b]
    staked = sum(b["stake"] for b in done)
    pnl = sum(b["pnl"] for b in done)
    wins = sum(b["status"] == "won" for b in done)
    print(f"\nSettled {len(done)} ({wins} won), staked ${staked:.2f}, P&L ${pnl:+.2f}"
          + (f" ({pnl / staked:+.1%})" if staked else "") + f". Open: {len(bets) - len(done)}.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ledger", type=Path, default=Path("paper/gameday.jsonl"))
    sub = parser.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("board")
    b.add_argument("--date", type=date.fromisoformat, help="game day, YYYY-MM-DD (default: this Saturday)")
    b.add_argument("--all", action="store_true", help="show every matched team, not just flagged plays")
    b.add_argument("--paper", action="store_true", help="log flagged plays as paper bets")
    b.add_argument("--csv", help="also write every play to this CSV")
    d = Settings()
    b.add_argument("--min-ev", type=float, default=d.min_ev)
    b.add_argument("--bankroll", type=float, default=d.bankroll)
    b.add_argument("--max-bet", type=float, default=d.max_bet)
    b.add_argument("--day-cap", type=float, default=d.day_cap)
    sub.add_parser("settle")
    args = parser.parse_args()

    if args.cmd == "board":
        s = Settings(min_ev=args.min_ev, bankroll=args.bankroll, max_bet=args.max_bet, day_cap=args.day_cap)
        board(args, s)
    else:
        show_record(args)


if __name__ == "__main__":
    main()
