# Kalshi bot

Research, backtesting and (eventually) execution for Kalshi prediction markets.
The research write-up behind this code: https://claude.ai/artifact/89RoCLY3Kw7hYSgsVn2KAq

## Stages

1. **Backtest** on collected Kalshi trades with real fees and a time split.
2. **Demo exchange** (`KALSHI_ENV=demo`, fake money) for a couple of weeks.
3. **Small live size**: $200–500, 5% daily loss cap, 20% drawdown kill switch.
4. **Scale** only after a month of live results that match the backtest.

## Layout

| Path | What it does |
| --- | --- |
| `kalshi_bot/fees.py` | Taker and maker fee formulas |
| `kalshi_bot/client.py` | REST client with request signing; refuses live orders unless `KALSHI_ALLOW_LIVE=1` |
| `kalshi_bot/collector.py` | Downloads settled markets and trades per series into Parquet |
| `kalshi_bot/data.py` | Normalizes API records; loads collected data |
| `kalshi_bot/backtest.py` | Event-driven trade replay with maker queue fills, taker slippage, settlement |
| `kalshi_bot/risk.py` | Daily loss cap, drawdown kill switch, per-market and per-event loss caps |
| `kalshi_bot/strategies/longshot.py` | Sells overpriced longshots with resting orders |
| `kalshi_bot/strategies/bracket.py` | Finds bracket ladders that don't add up to $1 after fees |
| `scripts/run_backtest.py` | Parameter search on older data, one out-of-sample run on newer data |
| `kalshi_bot/gameday.py`, `scripts/gameday.py` | College football game-day board: Kalshi prices vs the sportsbook line, paper bets |

## Usage

```bash
pip install -e '.[dev]'
pytest
python -m kalshi_bot.collector KXHIGHNY KXHIGHCHI --out data --since 2025-06-01
python scripts/run_backtest.py --data data --split 2026-07-01
```

## College football game day

```bash
python scripts/gameday.py board            # this Saturday: Kalshi plays priced below the book's fair line
python scripts/gameday.py board --all      # every matched game, both teams
python scripts/gameday.py board --paper    # log flagged plays as paper bets in paper/gameday.jsonl
python scripts/gameday.py settle           # settle paper bets from Kalshi results, print the record
python scripts/paper_trade.py --series KXNCAAFGAME --out paper   # longshot paper trader on CFB
```

For each FBS game, the DraftKings moneyline from ESPN's public scoreboard is devigged into
a fair win probability and compared with the cheapest way to back each team on Kalshi
(YES on the team or NO on its opponent). A play is flagged when fair / (ask + taker fee)
beats 1 + `--min-ev` (3% by default). `Limit` is the highest resting bid that still clears
that bar. Paper sizes are quarter Kelly on `--bankroll` ($200), capped at `--max-bet` ($10)
a play and `--day-cap` ($50) a run, one side per game, skipping prices outside 8–92¢ and
markets with under 100 contracts traded. No keys needed and no orders sent.

DraftKings has a margin and isn't the sharpest book, so treat small edges as noise. On the
first run (29 Oct) Kalshi tracked the book within a point or two on every FBS game.

## Credentials

Never commit keys or paste them in chat. Set them as environment variables
(in the cloud environment's settings for Claude sessions):

- `KALSHI_API_KEY_ID`
- `KALSHI_PRIVATE_KEY` (PEM text) or `KALSHI_PRIVATE_KEY_PATH`
- `KALSHI_ENV` = `demo` (default) or `prod`
- `KALSHI_ALLOW_LIVE` = `1` only once live trading is approved

## Backtest assumptions to keep in mind

- No order book history exists, so maker fills are inferred from later trades.
  A trade at exactly our price fills only `queue_fill_fraction` (default 50%) of its size.
- Taker orders fill at last trade ± 1¢ and no more than the last trade's size.
- Markets settle at `close_time`; real payouts can arrive later.
- Order-endpoint paths and fields follow Kalshi's docs as of Oct 2026 and need a
  check against the demo exchange before first use.
