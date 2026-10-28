# Kalshi bot

Research, backtesting and (eventually) execution for Kalshi prediction markets.
The research write-up behind this code: https://claude.ai/artifact/89RoCLY3Kw7hYSgsVn2KAq

The `kalshi_bot/sports` engine trades sports game-winner contracts on Kalshi and
Polymarket US, using sharp sportsbook lines only as fair value. Its strategy is the
Betting Edge Playbook: https://claude.ai/artifact/5yMzCVDdZ18CjthFKG5cGd
See [Sports engine](#sports-engine) below.

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
| `kalshi_bot/polymarket_us.py` | Polymarket US REST client with Ed25519 signing; refuses orders unless `POLYMARKET_US_ALLOW_LIVE=1` |
| `kalshi_bot/sports/` | Sports engine: sharp-line fair value, edge finder, Kelly sizing, CLV ledger |

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

## Sports engine

Prices NFL, NBA, MLB and NHL (plus college football and basketball if enabled) game-winner
contracts on Kalshi and Polymarket US against devigged Pinnacle moneylines, and buys
only contracts priced better than fair after the venue's fee. It never places a
sportsbook bet; sportsbook prices are read-only inputs.

```bash
python -m kalshi_bot.sports scan            # read-only list of mispriced contracts
python -m kalshi_bot.sports run --once      # one paper-trading cycle
python -m kalshi_bot.sports run --every 300 # paper trade every 5 minutes
python -m kalshi_bot.sports settle          # grade finished games
python -m kalshi_bot.sports report          # P&L and closing-line value
```

How it works, one cycle:

1. **Fair value.** Pull moneylines for the sharp books in `SPORTS_SHARP_BOOKS` from
   The Odds API, strip the margin with the power method, and average across books.
2. **Match.** Read each venue's game-winner contracts and pair them with games by team
   names and date. Pairings that aren't clear-cut are skipped, not guessed.
3. **Edge.** All-in cost is price plus fee: Kalshi taker `0.07·P·(1−P)`, Kalshi maker per
   the series' fee type, Polymarket US taker `0.05·P·(1−P)`, Polymarket US maker free.
   - Take the ask only if fair minus cost is at least `SPORTS_TAKER_MIN_EDGE` (3¢).
   - Otherwise rest a post-only bid one cent above the best bid, capped where fair minus
     cost is still `SPORTS_MAKER_MIN_EDGE` (1.5¢). Maker orders are the default path.
4. **Size.** Quarter Kelly, `(p − c) / (1 − c)` of bankroll times `SPORTS_KELLY_FRACTION`,
   capped so the worst-case loss on one game across venues stays under `SPORTS_MAX_GAME_PCT` (3%).
5. **Manage.** Each cycle cancels resting bids whose edge disappeared after the sharp line
   moved, reprices when outbid, and cancels everything 5 minutes before the start.
   Pre-game only for now.
6. **Ledger.** Every order goes to SQLite (`data/sports_ledger.sqlite`). The last fair value
   before the start is the closing line, and every fill gets `CLV = closing fair − cost`.
   Positive average CLV is the early sign the process works, long before P&L is reliable.

Paper mode is the default. A paper maker bid counts as filled when the venue's ask drops
to its price, which ignores queue position, so treat paper maker results as approximate.

### Sports settings (environment variables)

| Variable | Default | Meaning |
| --- | --- | --- |
| `ODDS_API_KEY` | none, required | The Odds API key |
| `SPORTS_VENUES` | `kalshi,polymarket` | Drop a venue that isn't legal or funded in your state |
| `SPORTS_LEAGUES` | `nfl,nba,mlb,nhl` | Also `ncaaf`, `ncaab` |
| `SPORTS_SHARP_BOOKS` | `pinnacle` | The Odds API bookmaker keys to devig and average |
| `SPORTS_MODE` | `paper` | `live` also needs each venue's allow-live flag |
| `SPORTS_BANKROLL` | `500` | Dollars |
| `SPORTS_KELLY_FRACTION` | `0.25` | Quarter Kelly |
| `SPORTS_MAKER_MIN_EDGE` / `SPORTS_TAKER_MIN_EDGE` | `0.015` / `0.03` | Dollars per contract after fees |
| `SPORTS_MAX_GAME_PCT` | `0.03` | Worst-case loss on one game, share of bankroll |
| `SPORTS_LEDGER` | `data/sports_ledger.sqlite` | Ledger path (git-ignored) |
| `POLYMARKET_US_KEY_ID`, `POLYMARKET_US_SECRET_KEY` | none | Polymarket US API key, live mode only |
| `POLYMARKET_US_ALLOW_LIVE` | unset | `1` to allow real Polymarket US orders |

Kalshi uses the credentials listed above. Live Kalshi orders go to `KALSHI_ENV` (demo by
default) while market data always comes from prod, so demo testing needs demo tickers to match.

### Before going live

- Check each league's Kalshi series ticker and Polymarket US league slug in
  `kalshi_bot/sports/config.py` against the venues, and run `scan` to confirm games match.
- Polymarket US order, cancel and order-status paths follow docs.polymarket.us (Oct 2026)
  but haven't been exercised. In particular, confirm that a `BUY_SHORT` limit price is the
  NO price before trading the second team on Polymarket. Start with one 1-contract order.
- Read each contract's settlement rules. Overtime, postponements and ties can settle
  differently from a sportsbook moneyline.
- Sports contracts face legal challenges in several states. Confirm your state's status
  and set `SPORTS_VENUES` to match.
- Watch paper CLV for a few weeks first. If average CLV isn't positive, the edge isn't real.
