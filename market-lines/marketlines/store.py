"""Storage: SQLite locally, Postgres (Supabase) in production.

MARKETLINES_DB is a file path for SQLite or a postgres:// URL for Supabase (the
"Session pooler" connection string). Times are stored as ISO-8601 UTC text so the
same SQL runs on both.

Snapshots are change-only: a row is written when a book's number or price moves,
or with empty prices when the book pulls the market. Each poll is logged in
`polls`, so the full snapshot history at every poll can be rebuilt from the two.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = (Path(__file__).parent / "schema.sql").read_text()


def iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_ts(value) -> datetime:
    if not isinstance(value, datetime):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class Store:
    def __init__(self, url: str | None = None):
        url = url or os.environ.get("MARKETLINES_DB", "marketlines.db")
        self.postgres = url.startswith(("postgres://", "postgresql://"))
        if self.postgres:
            import psycopg  # only needed against Supabase

            self.conn = psycopg.connect(url, autocommit=False)
        else:
            import sqlite3

            self.conn = sqlite3.connect(url)
        self.init()

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.postgres else sql

    def execute(self, sql: str, params=()):
        cur = self.conn.cursor()
        cur.execute(self._sql(sql), params)
        return cur

    def executemany(self, sql: str, rows):
        rows = list(rows)
        if rows:
            self.conn.cursor().executemany(self._sql(sql), rows)

    def query(self, sql: str, params=()) -> list[tuple]:
        return self.execute(sql, params).fetchall()

    def commit(self):
        self.conn.commit()

    def init(self):
        sql = "\n".join(line.split("--")[0] for line in SCHEMA.splitlines())
        for statement in sql.split(";"):
            if statement.strip():
                self.execute(statement)
        self.commit()

    # Events and polls

    def upsert_events(self, events):
        self.executemany(
            """INSERT INTO events (event_id, sport, commence_time, home, away) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (event_id) DO UPDATE SET commence_time = excluded.commence_time""",
            [(e.event_id, e.sport, iso(e.commence), e.home, e.away) for e in events],
        )

    def log_poll(self, sport: str, ts: datetime, events: int, credits_remaining: int | None, source: str):
        self.execute(
            "INSERT INTO polls (sport, ts, events, credits_remaining, source) VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
            (sport, iso(ts), events, credits_remaining, source),
        )

    def previous_poll(self, sport: str, before: datetime) -> datetime | None:
        row = self.query("SELECT MAX(ts) FROM polls WHERE sport = ? AND ts < ?", (sport, iso(before)))
        return parse_ts(row[0][0]) if row and row[0][0] else None

    def events(self, sport: str | None = None, ids=None) -> list[tuple]:
        sql, params = "SELECT event_id, sport, commence_time, home, away FROM events WHERE 1=1", []
        if sport:
            sql += " AND sport = ?"
            params.append(sport)
        if ids is not None:
            ids = list(ids)
            if not ids:
                return []
            sql += f" AND event_id IN ({','.join('?' * len(ids))})"
            params.extend(ids)
        return [(r[0], r[1], parse_ts(r[2]), r[3], r[4]) for r in self.query(sql, params)]

    # Snapshots

    def latest_quotes(self, event_ids) -> dict[tuple, tuple]:
        """(event, book, market) -> (point, price_a, price_b) as last stored."""
        ids = list(event_ids)
        if not ids:
            return {}
        rows = self.query(
            f"""SELECT s.event_id, s.book, s.market, s.point, s.price_a, s.price_b FROM snapshots s
                JOIN (SELECT event_id, book, market, MAX(ts) AS ts FROM snapshots
                      WHERE event_id IN ({','.join('?' * len(ids))}) GROUP BY event_id, book, market) m
                ON s.event_id = m.event_id AND s.book = m.book AND s.market = m.market AND s.ts = m.ts""",
            ids,
        )
        return {(r[0], r[1], r[2]): (r[3], r[4], r[5]) for r in rows}

    def insert_snapshots(self, rows):
        """rows: (event_id, book, market, ts, point, price_a, price_b, book_update)."""
        self.executemany(
            """INSERT INTO snapshots (event_id, book, market, ts, point, price_a, price_b, book_update)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING""",
            [(e, b, m, iso(ts), p, a, bb, iso(u) if u else None) for e, b, m, ts, p, a, bb, u in rows],
        )

    def timeline_rows(self, event_id: str, market: str) -> list[tuple]:
        rows = self.query(
            "SELECT ts, book, point, price_a, price_b FROM snapshots WHERE event_id = ? AND market = ? ORDER BY ts",
            (event_id, market),
        )
        return [(parse_ts(r[0]), r[1], r[2], r[3], r[4]) for r in rows]

    # Betting splits (optional, for true RLM)

    def ticket_share(self, event_id: str, market: str, at: datetime) -> float | None:
        rows = self.query(
            "SELECT ticket_share_a FROM splits WHERE event_id = ? AND market = ? AND ts <= ? ORDER BY ts DESC LIMIT 1",
            (event_id, market, iso(at)),
        )
        return rows[0][0] if rows else None

    def insert_splits(self, rows):
        """rows: (event_id, market, ts, ticket_share_a, money_share_a, source)."""
        self.executemany(
            """INSERT INTO splits (event_id, market, ts, ticket_share_a, money_share_a, source)
               VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING""",
            [(e, m, iso(ts), t, mo, s) for e, m, ts, t, mo, s in rows],
        )

    # Signals and the CLV ledger

    def insert_signals(self, signals) -> list:
        """Store signals not already logged; returns the new ones (for alerts)."""
        signals = list({s.key: s for s in signals}.values())
        if not signals:
            return []
        keys = [s.key for s in signals]
        seen = {r[0] for r in self.query(
            f"SELECT signal_key FROM signals WHERE signal_key IN ({','.join('?' * len(keys))})", keys)}
        new = [s for s in signals if s.key not in seen]
        self.executemany(
            """INSERT INTO signals (signal_key, event_id, sport, market, kind, side, book, line, price, fair,
                                    cons_point, cons_fair_a, score, fired_at, detail)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(s.key, s.event_id, s.sport, s.market, s.kind, s.side_name, s.book, s.line, s.price, s.fair,
              s.cons_point, s.cons_fair_a, s.score, iso(s.fired_at), json.dumps(s.detail)) for s in new],
        )
        return new

    def save_close(self, event_id: str, market: str, cons, captured_at: datetime):
        self.execute(
            """INSERT INTO closes (event_id, market, point, fair_a, source, books, captured_at)
               VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (event_id, market) DO UPDATE SET
               point = excluded.point, fair_a = excluded.fair_a, source = excluded.source,
               books = excluded.books, captured_at = excluded.captured_at""",
            (event_id, market, cons.point, cons.fair_a, cons.source, cons.books, iso(captured_at)),
        )

    def events_needing_close(self, now: datetime) -> list[tuple]:
        """Started events with a market that has snapshots but no close yet."""
        rows = self.query(
            """SELECT DISTINCT e.event_id, e.sport, e.commence_time, s.market FROM events e
               JOIN snapshots s ON s.event_id = e.event_id
               LEFT JOIN closes c ON c.event_id = e.event_id AND c.market = s.market
               WHERE e.commence_time <= ? AND c.event_id IS NULL""",
            (iso(now),),
        )
        return [(r[0], r[1], parse_ts(r[2]), r[3]) for r in rows]

    def signals_needing_clv(self) -> list[tuple]:
        return self.query(
            """SELECT s.signal_key, s.sport, s.market, s.side, s.line, s.price, s.cons_point, s.cons_fair_a,
                      c.point, c.fair_a, c.source, c.books
               FROM signals s JOIN closes c ON c.event_id = s.event_id AND c.market = s.market
               WHERE s.clv_ev IS NULL"""
        )

    def set_clv(self, rows):
        """rows: (clv_ev, clv_move, clv_points, close_point, close_fair, signal_key)."""
        self.executemany(
            """UPDATE signals SET clv_ev = ?, clv_move = ?, clv_points = ?, close_point = ?, close_fair = ?
               WHERE signal_key = ?""",
            rows,
        )

    def ledger(self, since: datetime) -> list[tuple]:
        return self.query(
            """SELECT sport, kind, market, clv_ev, clv_move, clv_points, score FROM signals
               WHERE clv_ev IS NOT NULL AND fired_at >= ?""",
            (iso(since),),
        )

    def recent_signals(self, since: datetime, min_score: float = 0) -> list[tuple]:
        return self.query(
            """SELECT s.fired_at, s.sport, e.away, e.home, s.market, s.kind, s.side, s.book, s.line, s.price,
                      s.fair, s.score, s.clv_ev
               FROM signals s JOIN events e ON e.event_id = s.event_id
               WHERE s.fired_at >= ? AND s.score >= ? ORDER BY s.fired_at""",
            (iso(since), min_score),
        )
