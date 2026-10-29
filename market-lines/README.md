# marketlines

Line-movement signals for college football, college basketball and (from February)
college baseball, with a closing-line-value ledger for every signal. It reads
sportsbook odds only. It never places a bet and sells no picks: it is the market
instrument behind the signal feed, the public CLV ledger and alerts.

This folder stands alone so it can move into its own repository (`git subtree split
--prefix market-lines`).

## What it does

Every poll pulls moneyline, spread and total prices from up to ten books, stores
whatever changed, and runs the detectors on each market that moved:

| Signal | Fires when |
| --- | --- |
| `steam` | 3+ books move the same way by 2+ points of probability between polls under 30 minutes apart, and none move the other way. Names the book that hasn't moved yet. |
| `key` | The consensus spread lands on or crosses 3, 7, 10, 14 or 17 (football only). |
| `drift` | The consensus has moved another 4 points of probability since the open. |
| `rlm` | 60%+ of tickets are on one side and the line moved 3+ points the other way. Needs a betting-splits source (`splits` command); none is wired up yet. |
| `rlm_proxy` | No splits: a 3+ point move toward the underdog or the under, where public money rarely goes. Logged separately so its CLV is judged on its own. |
| `divergence` | One book's number is +2.5% EV or better against the sharp consensus (Pinnacle if it quotes the game, else the median of the other books). |
| `model` | The ratings model below prices a book's number at +4% EV or better. Fires once per side, and is judged on CLV like the rest. |

Moves are measured in probability rather than points so one rule covers
moneylines, spreads and totals and so a juice move counts as much as a number
move. Spread and total lines are converted with a table of how often each final
margin happens (`sports.py`). Football's table weights 3, 7, 10, 14 and 17. These
tables are starting estimates; refit them from final scores once there is a season
of data.

Every signal records the book and number available at that moment, its fair
probability, and a 0–100 score. When the game starts, the last pre-game quotes
become the close and each signal gets:

* `clv_ev`: fair probability at the close, at the signal's number, times its price, minus 1. This is the headline ledger number.
* `clv_move`: how far the consensus moved in the signal's direction from signal to close. It scores the prediction without credit for line shopping.
* `clv_points`: points gained on the number (spreads, totals).

`ledger` prints rolling CLV by sport and signal type and marks a signal type
`retire` once it has 30+ settled signals and a negative average, which is the rule
from the plan.

## The game model

`model.py` rates every team's offense and defense from final scores with a weighted
ridge regression: each team's points = league average + its offense − the
opponent's defense ± half the home edge (no edge at neutral sites). Recent games
count more (half-life 120 days in football, 60 in basketball), and last season's
games enter at a decayed weight, which works as a preseason prior. Ridge
shrinkage keeps teams with few games near average.

That gives a projected margin and total for any matchup. The spread of real results
around past projections (measured out of sample) sets the sigma that turns them into
win, cover and over probabilities. Those projections are the Quant tier's "model
line next to the market" and drive the `model` signal.

The same scores refit the margin and total tables that convert line moves into
probability, so `key` and every point-based comparison use real frequencies
instead of hand estimates once 300+ games are stored.

```bash
python -m marketlines scores --sport ncaaf --start 2025-08-23 --end 2026-11-08   # two seasons + next week's neutral sites
python -m marketlines scores --sport ncaab --start 2024-11-04 --end 2026-11-08
python -m marketlines evaluate --sport ncaaf --start 2025-09-20 --end 2026-01-20 --half-life 60 120 200 --ridge 2 4 8
python -m marketlines fit                 # refit, refit tables, project upcoming games
python -m marketlines ratings --sport ncaaf
python -m marketlines projections         # model line, market line, difference, win %
```

`run` collects new scores and refits once a day on its own. `evaluate` is a
walk-forward test (refit weekly on earlier games only, score the next week) for
tuning half-life and ridge. The honest bar for the model is not winners picked but
whether `model` signals beat the close in the ledger.

## Run it

```bash
cd market-lines
pip install -e '.[dev]'            # add [postgres] for Supabase
export ODDS_API_KEY=...            # https://the-odds-api.com, never commit it
python -m marketlines poll --sports ncaaf,ncaab
python -m marketlines signals --hours 24
python -m marketlines ledger --days 90
python -m marketlines run --sports ncaaf,ncaab     # long-running poller
```

`--db` (before the command) or `MARKETLINES_DB` picks the database: a file path for
SQLite, or the Supabase Postgres connection string. The tables are created on first
run from `marketlines/schema.sql`; that file also works pasted into the Supabase SQL
editor.

### Schedule and credit budget

On The Odds API a live call costs markets x regions, and up to ten named books count
as one region, so a poll is 3 credits per sport. `run` paces itself:

* 5 minutes inside an hour of the next kickoff, 10 within 3 hours, 20 within 12, hourly within two days, every 3 hours otherwise.
* Never faster than three times the rate that would spend the remaining credits evenly by month end.
* An extra poll about 4 minutes before each kickoff, so the close is fresh.

The free tier (500 credits a month) covers roughly 80 polls per sport, enough to
test the pipeline but not to catch steam. Use a paid tier for the live feed.

For a host without a long-running process, `deploy/` has a cron-style GitHub
Actions workflow that runs `poll` against Supabase every 10 minutes. Copy it into
the new repository's `.github/workflows/` and set `ODDS_API_KEY` and
`MARKETLINES_DB` as repository secrets.

### Backfilling this season

```bash
python -m marketlines backfill --sport ncaaf --start 2026-08-23 --step 3h --dry-run
```

`--dry-run` prints the credit cost first (historical snapshots cost ten times a live
call and need a paid plan; this season at a 3-hour step is about 16,000 credits,
daily is about 2,000). Backfilled snapshots go through the same pipeline as live
ones, so drift, key, divergence and RLM-proxy signals and their CLV are filled in.
Steam needs polls under 30 minutes apart, so a coarse backfill will not produce it.

## Layout

| Path | What it does |
| --- | --- |
| `marketlines/feed.py` | The Odds API client (live and historical) and response parsing |
| `marketlines/lines.py` | Book lines, point-to-probability conversion, consensus, timelines |
| `marketlines/signals.py` | The six detectors |
| `marketlines/clv.py` | Closing lines and per-signal CLV |
| `marketlines/engine.py` | Ingest a snapshot, settle closes, pacing, backfill |
| `marketlines/results.py` | Final scores and neutral sites from ESPN's scoreboard |
| `marketlines/model.py` | Ratings fit, probabilities, walk-forward check, table refits |
| `marketlines/projections.py` | Collect scores, refit, project upcoming feed events |
| `marketlines/teams.py` | Team-name matching between the odds feed and ESPN |
| `marketlines/store.py`, `schema.sql` | SQLite / Supabase storage |
| `marketlines/sports.py` | Per-sport books, key numbers, margin tables and thresholds |

## Not done yet

* No live run yet: the build sandbox can't reach the odds API or ESPN, so the response parsing is written to their documented formats and tested on fixtures, and the model is tested on simulated seasons. First real run: `scores`, `evaluate`, `fit`, then `run`.
* The model uses scores only. Box-score features (yards per play, tempo, injuries) would sharpen it and are the next step once its CLV is known.
* No betting-splits source, so `rlm` stays silent and `rlm_proxy` stands in.
* Alerts, the public ledger page and Stripe are weeks 3–4.
