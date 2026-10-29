-- Runs on SQLite and on Postgres (Supabase). Times are ISO-8601 UTC text.
-- Prices are decimal odds. Side A is home (h2h, spreads) or over (totals).

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    sport TEXT NOT NULL,
    commence_time TEXT NOT NULL,
    home TEXT NOT NULL,
    away TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS polls (
    sport TEXT NOT NULL,
    ts TEXT NOT NULL,
    events INTEGER NOT NULL,
    credits_remaining INTEGER,
    source TEXT NOT NULL,           -- live or historical
    PRIMARY KEY (sport, ts)
);

-- Change-only: a row when a book's number or price changes, empty prices = pulled.
CREATE TABLE IF NOT EXISTS snapshots (
    event_id TEXT NOT NULL,
    book TEXT NOT NULL,
    market TEXT NOT NULL,           -- h2h, spreads, totals
    ts TEXT NOT NULL,               -- poll time
    point DOUBLE PRECISION,         -- home spread or total; null for h2h
    price_a DOUBLE PRECISION,
    price_b DOUBLE PRECISION,
    book_update TEXT,               -- the book's own last_update from the feed
    PRIMARY KEY (event_id, book, market, ts)
);

CREATE INDEX IF NOT EXISTS snapshots_event_market ON snapshots (event_id, market, ts);

-- Public betting splits, if a source is added. Needed for true RLM.
CREATE TABLE IF NOT EXISTS splits (
    event_id TEXT NOT NULL,
    market TEXT NOT NULL,
    ts TEXT NOT NULL,
    ticket_share_a DOUBLE PRECISION,
    money_share_a DOUBLE PRECISION,
    source TEXT,
    PRIMARY KEY (event_id, market, ts)
);

CREATE TABLE IF NOT EXISTS closes (
    event_id TEXT NOT NULL,
    market TEXT NOT NULL,
    point DOUBLE PRECISION,
    fair_a DOUBLE PRECISION NOT NULL,
    source TEXT NOT NULL,           -- sharp book used, or median
    books INTEGER NOT NULL,
    captured_at TEXT NOT NULL,
    PRIMARY KEY (event_id, market)
);

CREATE TABLE IF NOT EXISTS signals (
    signal_key TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    sport TEXT NOT NULL,
    market TEXT NOT NULL,
    kind TEXT NOT NULL,             -- steam, key, drift, rlm, rlm_proxy, divergence
    side TEXT NOT NULL,             -- home, away, over, under
    book TEXT NOT NULL,             -- where the number was available
    line DOUBLE PRECISION,          -- that book's home spread or total
    price DOUBLE PRECISION NOT NULL,
    fair DOUBLE PRECISION NOT NULL, -- consensus fair probability of the side at that line
    cons_point DOUBLE PRECISION,
    cons_fair_a DOUBLE PRECISION NOT NULL,
    score DOUBLE PRECISION NOT NULL,
    fired_at TEXT NOT NULL,
    detail TEXT,
    close_point DOUBLE PRECISION,
    close_fair DOUBLE PRECISION,
    clv_ev DOUBLE PRECISION,
    clv_move DOUBLE PRECISION,
    clv_points DOUBLE PRECISION
);

CREATE INDEX IF NOT EXISTS signals_fired ON signals (fired_at);
CREATE INDEX IF NOT EXISTS signals_event ON signals (event_id, market)
