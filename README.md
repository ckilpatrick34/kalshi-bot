# Kalshi bot

Research, backtesting and (eventually) execution for Kalshi prediction markets.
The research write-up behind this code: https://claude.ai/artifact/89RoCLY3Kw7hYSgsVn2KAq

The `kalshi_bot/sports` tool models upcoming games and lists sports contracts on Kalshi
and Polymarket US that are priced below the model's win probability. It only advises
and never places orders. See [Sports picks](#sports-picks) below. Background research:
https://claude.ai/artifact/5yMzCVDdZ18CjthFKG5cGd

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
| `kalshi_bot/polymarket_us.py` | Read-only Polymarket US market data (no orders) |
| `kalshi_bot/sports/` | Sports picks: Elo game model, venue prices, EV after fees, model backtest |

## Usage

```bash
pip install -e '.[dev]'
pytest
python -m kalshi_bot.collector KXHIGHNY KXHIGHCHI --out data --since 2025-06-01
python scripts/run_backtest.py --data data --split 2026-07-01
```

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

## Sports picks

Gives you win percentages for upcoming games and the plays where Kalshi or Polymarket US
prices a team below that percentage after fees. You decide what, if anything, to bet.
Nothing here places an order.

```bash
python -m kalshi_bot.sports picks             # flagged high-EV plays
python -m kalshi_bot.sports picks --all       # every priced contract, with model %
python -m kalshi_bot.sports picks --csv picks.csv
python -m kalshi_bot.sports backtest          # how accurate the model has been
python -m kalshi_bot.sports ratings nba       # current team ratings
```

No keys are needed: results come from ESPN's public scoreboard, and prices from Kalshi's
and Polymarket US's public market data. The first run downloads a few seasons of results
and caches finished months under `data/results/`.

How it works:

1. **Model.** An Elo rating per team per league, learned from every game in the last
   `SPORTS_HISTORY_YEARS` seasons. Home advantage, margin of victory and an offseason pull
   back toward average follow FiveThirtyEight's published Elo settings. The home team's win
   probability is `1 / (1 + 10^(-(home + home advantage - away) / 400))`.
2. **Prices.** Each venue's game-winner contracts are matched to the games by team names and
   date. The ask is the price you would pay right now.
3. **EV.** Cost = ask + taker fee (Kalshi `0.07·P·(1−P)`, Polymarket US `0.05·P·(1−P)`).
   `Edge = model % − cost`, `EV = model % / cost − 1` per $1 staked. Plays with EV at or above
   `SPORTS_MIN_EV` (5%) are flagged with `*`. A resting limit order below the ask
   avoids most fees and does better than this.
4. **Stake.** A quarter-Kelly suggestion on `SPORTS_BANKROLL`, capped at
   `SPORTS_MAX_GAME_PCT` of bankroll per game.
5. **Second opinion (optional).** With `ODDS_API_KEY` set, each play also shows the devigged
   Pinnacle probability. A play where the model and the sharp line both beat the price is
   much stronger than one where only the model does.

### How much to trust the percentages

Run `backtest` first. It replays history and scores every prediction made before the game,
reporting accuracy, log loss against a pick-the-home-team baseline, and calibration (when
the model said 70%, how often did that team win). Elo knows only results: it doesn't know
about injuries, rest, starting pitchers or goalies, or weather. The betting markets do.
So a big gap between the model and the price often means the market knows something the
model doesn't. Treat flagged plays as a shortlist to check, not as bets, and prefer the
ones where the sharp line agrees. Plays marked `(thin data)` involve a team with fewer
than 8 games in the model. They are never flagged.

### Settings (environment variables)

| Variable | Default | Meaning |
| --- | --- | --- |
| `SPORTS_LEAGUES` | `nfl,nba,mlb,nhl` | Also `ncaaf`, `ncaab` |
| `SPORTS_VENUES` | `kalshi,polymarket` | Drop a venue that isn't legal or funded in your state |
| `SPORTS_MIN_EV` | `0.05` | EV per $1 after fees needed to flag a play |
| `SPORTS_BANKROLL` | `500` | Dollars, for the suggested stake only |
| `SPORTS_KELLY_FRACTION` | `0.25` | Quarter Kelly |
| `SPORTS_MAX_GAME_PCT` | `0.03` | Suggested stake cap per game |
| `SPORTS_DAYS_AHEAD` | `3` | How far ahead to look for games |
| `SPORTS_HISTORY_YEARS` | `3` | Seasons of results the model learns from |
| `ODDS_API_KEY` | unset | Optional, adds the sharp-line column (free tier at the-odds-api.com) |

Kalshi series tickers and Polymarket US league slugs are in `kalshi_bot/sports/config.py`.
They follow the venues as of Oct 2026 and haven't yet been checked against live responses.
Check the per-league "priced N of M games" lines that `picks` prints.
