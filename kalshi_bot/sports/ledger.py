"""SQLite trade ledger with closing-line value.

Every order the engine places (paper or live) is a row in `orders`. Every scan stores
the sharp fair value of each game in `fair_snapshots`; once a game starts, the last
snapshot before the start is its closing fair value, and each filled order on that
game gets its CLV:

    clv = closing fair - (price + fee per contract)

Positive average CLV means the engine is buying below where the sharp market closed,
which shows up long before win/loss results become meaningful.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    game_id TEXT PRIMARY KEY, league TEXT, home TEXT, away TEXT, commence TEXT,
    closing_home_fair REAL, winner TEXT
);
CREATE TABLE IF NOT EXISTS fair_snapshots (
    game_id TEXT, ts TEXT, home_fair REAL, books TEXT
);
CREATE INDEX IF NOT EXISTS fair_snapshots_game ON fair_snapshots (game_id, ts);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created TEXT, mode TEXT, venue TEXT, market_id TEXT, side TEXT,
    game_id TEXT, league TEXT, team TEXT, kind TEXT,
    price REAL, contracts REAL, fee REAL, fair REAL,
    status TEXT, filled_at TEXT, venue_order_id TEXT,
    closing_fair REAL, clv REAL, result TEXT, pnl REAL
);
"""


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).isoformat()


class Ledger:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # --- games and fair values ---------------------------------------------

    def record_game(self, game, now: datetime) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO games (game_id, league, home, away, commence) VALUES (?, ?, ?, ?, ?)",
            (game.id, game.league, game.home, game.away, _iso(game.commence)),
        )
        if game.commence > now:
            self.db.execute(
                "INSERT INTO fair_snapshots VALUES (?, ?, ?, ?)",
                (game.id, _iso(now), game.fair[game.home], ",".join(game.books)),
            )
        self.db.commit()

    def close_started_games(self, now: datetime) -> int:
        """Set closing fair value and CLV for games that have started. Returns how many were closed."""
        rows = self.db.execute(
            "SELECT * FROM games WHERE closing_home_fair IS NULL AND commence <= ?", (_iso(now),)
        ).fetchall()
        closed = 0
        for g in rows:
            snap = self.db.execute(
                "SELECT home_fair FROM fair_snapshots WHERE game_id = ? AND ts <= ? ORDER BY ts DESC LIMIT 1",
                (g["game_id"], g["commence"]),
            ).fetchone()
            if not snap:
                continue
            home_fair = snap["home_fair"]
            self.db.execute("UPDATE games SET closing_home_fair = ? WHERE game_id = ?", (home_fair, g["game_id"]))
            for o in self.db.execute("SELECT * FROM orders WHERE game_id = ?", (g["game_id"],)).fetchall():
                closing = home_fair if o["team"] == g["home"] else 1 - home_fair
                clv = closing - (o["price"] + o["fee"] / o["contracts"]) if o["status"] == "filled" else None
                self.db.execute("UPDATE orders SET closing_fair = ?, clv = ? WHERE id = ?", (closing, clv, o["id"]))
            closed += 1
        self.db.commit()
        return closed

    def settle(self, game_id: str, winner: str) -> None:
        self.db.execute("UPDATE games SET winner = ? WHERE game_id = ?", (winner, game_id))
        for o in self.db.execute("SELECT * FROM orders WHERE game_id = ? AND status = 'filled'", (game_id,)).fetchall():
            won = o["team"] == winner
            pnl = (1.0 if won else 0.0) * o["contracts"] - o["price"] * o["contracts"] - o["fee"]
            self.db.execute(
                "UPDATE orders SET result = ?, pnl = ? WHERE id = ?", ("win" if won else "loss", round(pnl, 4), o["id"])
            )
        self.db.commit()

    def unsettled_games(self, now: datetime) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM games WHERE winner IS NULL AND commence <= ? AND game_id IN "
            "(SELECT game_id FROM orders WHERE status = 'filled')",
            (_iso(now),),
        ).fetchall()

    # --- orders -------------------------------------------------------------

    def add_order(self, now: datetime, mode: str, matched, decision, status: str, venue_order_id: str | None) -> int:
        c = matched.contract
        cur = self.db.execute(
            "INSERT INTO orders (created, mode, venue, market_id, side, game_id, league, team, kind, price, contracts,"
            " fee, fair, status, filled_at, venue_order_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _iso(now), mode, c.venue, c.market_id, c.side, matched.game.id, matched.game.league, matched.team,
                decision.kind, decision.price, decision.contracts, decision.fee, decision.fair, status,
                _iso(now) if status == "filled" else None, venue_order_id,
            ),
        )
        self.db.commit()
        return cur.lastrowid

    def fill(self, order_id: int, now: datetime, contracts: float | None = None) -> None:
        if contracts is not None:
            self.db.execute(
                "UPDATE orders SET contracts = ?, fee = fee * ? / contracts WHERE id = ?", (contracts, contracts, order_id)
            )
        self.db.execute("UPDATE orders SET status = 'filled', filled_at = ? WHERE id = ?", (_iso(now), order_id))
        self.db.commit()

    def cancel(self, order_id: int) -> None:
        self.db.execute("UPDATE orders SET status = 'cancelled' WHERE id = ?", (order_id,))
        self.db.commit()

    def resting(self, mode: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM orders WHERE status = 'resting' AND mode = ?", (mode,)).fetchall()

    def game_exposure(self, game_id: str, mode: str) -> float:
        """Worst-case loss on a game: everything paid or committed on it, as if every leg loses."""
        row = self.db.execute(
            "SELECT COALESCE(SUM(price * contracts + fee), 0) FROM orders"
            " WHERE game_id = ? AND mode = ? AND status IN ('resting', 'filled')",
            (game_id, mode),
        ).fetchone()
        return row[0]

    # --- reporting ------------------------------------------------------------

    def report(self, mode: str | None = None) -> dict:
        where = "status = 'filled'" + (" AND mode = ?" if mode else "")
        args = (mode,) if mode else ()

        def summarize(group: str | None):
            cols = f"{group}, " if group else ""
            sql = (
                f"SELECT {cols}COUNT(*) n, SUM(contracts) contracts, SUM(price * contracts + fee) staked,"
                " SUM(pnl) pnl, SUM(CASE WHEN result IS NOT NULL THEN 1 ELSE 0 END) settled,"
                " SUM(clv * contracts) / NULLIF(SUM(CASE WHEN clv IS NOT NULL THEN contracts END), 0) avg_clv,"
                " SUM(clv * contracts) / NULLIF(SUM(CASE WHEN clv IS NOT NULL THEN price * contracts + fee END), 0)"
                " clv_pct, SUM((fair - price) * contracts - fee) expected"
                f" FROM orders WHERE {where}" + (f" GROUP BY {group} ORDER BY {group}" if group else "")
            )
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

        return {
            "overall": summarize(None)[0],
            "by_league": summarize("league"),
            "by_venue": summarize("venue"),
            "by_kind": summarize("kind"),
        }
