"""One poll: store what changed, run the detectors, capture closes, score CLV."""

import calendar
import logging
from datetime import datetime, timedelta, timezone

from .clv import closing, signal_clv
from .feed import Event, Quote, parse_events
from .lines import Consensus, Timeline
from .signals import Signal, detect
from .sports import SPORTS, Sport
from .store import Store

log = logging.getLogger(__name__)


def ingest(store: Store, sport: Sport, events: list[Event], quotes: list[Quote], ts: datetime,
           source: str = "live", credits_remaining: int | None = None) -> list[Signal]:
    """Record one feed snapshot taken at `ts` and return the new signals it produced.

    Only pre-game prices count: events that have started are dropped, so the last
    stored quote before the start is the close.
    """
    events = [e for e in events if e.commence > ts]
    ids = {e.event_id for e in events}
    quotes = [q for q in quotes if q.event_id in ids and q.market in sport.markets]
    store.upsert_events(events)

    last = store.latest_quotes(ids)
    current = {(q.event_id, q.book, q.market): q for q in quotes}
    rows, changed = [], set()
    for key, q in current.items():
        if last.get(key) != (q.point, q.price_a, q.price_b):
            rows.append((q.event_id, q.book, q.market, ts, q.point, q.price_a, q.price_b, q.book_update))
            changed.add((q.event_id, q.market))
    for (event_id, book, market), (_point, price_a, _price_b) in last.items():
        if price_a is not None and (event_id, book, market) not in current and market in sport.markets:
            rows.append((event_id, book, market, ts, None, None, None, None))  # book pulled the market
            changed.add((event_id, market))
    store.insert_snapshots(rows)
    t0 = store.previous_poll(sport.name, ts)
    store.log_poll(sport.name, ts, len(events), credits_remaining, source)

    found = []
    for event_id, market in sorted(changed):
        tl = Timeline(store.timeline_rows(event_id, market))
        found += detect(sport, market, event_id, tl, t0, ts, store.ticket_share(event_id, market, ts),
                        store.projection(event_id))
    new = store.insert_signals(found)
    store.commit()
    log.info("%s %s: %d events, %d changed lines, %d new signals", sport.name, ts.isoformat(), len(events),
             len(rows), len(new))
    return new


def settle(store: Store, now: datetime) -> tuple[int, int]:
    """Capture closing lines for started events, then score CLV on their signals."""
    closes = 0
    for event_id, sport_name, commence, market in store.events_needing_close(now):
        sport = SPORTS[sport_name]
        cons = closing(sport, market, Timeline(store.timeline_rows(event_id, market)), commence)
        if cons is None:
            continue
        store.save_close(event_id, market, cons, store.previous_poll(sport_name, commence) or commence)
        closes += 1

    rows = []
    for (key, sport_name, market, side, line, price, cons_point, cons_fair_a,
         close_point, close_fair_a, source, books) in store.signals_needing_clv():
        sport = SPORTS[sport_name]
        close = Consensus(close_point, close_fair_a, source, books)
        side_code = "a" if side in ("home", "over") else "b"
        clv = signal_clv(sport, market, side_code, line, price, cons_point, cons_fair_a, close)
        close_fair = close_fair_a if side_code == "a" else 1 - close_fair_a
        rows.append((clv.clv_ev, clv.clv_move, clv.clv_points, close_point, close_fair, key))
    store.set_clv(rows)
    store.commit()
    return closes, len(rows)


def poll(store: Store, api, sport: Sport, now: datetime | None = None) -> list[Signal]:
    now = now or datetime.now(timezone.utc)
    payload = api.odds(sport.feed_key, sport.markets, sport.books)
    events, quotes = parse_events(payload, sport.name)
    new = ingest(store, sport, events, quotes, now, "live", api.credits_remaining)
    settle(store, now)
    return new


def backfill(store: Store, api, sport: Sport, start: datetime, end: datetime, step: timedelta) -> int:
    """Replay historical snapshots through the same pipeline. Returns credits used."""
    used, at = 0, start
    while at <= end:
        snap_ts, payload = api.historical_odds(sport.feed_key, sport.markets, sport.books, at)
        events, quotes = parse_events(payload, sport.name)
        ingest(store, sport, events, quotes, snap_ts, "historical", api.credits_remaining)
        settle(store, snap_ts)
        used += api.last_cost or 0
        at += step
    settle(store, end)
    return used


def live_cost(sport: Sport) -> int:
    return len(sport.markets) * max(1, -(-len(sport.books) // 10))


def backfill_cost(sport: Sport, start: datetime, end: datetime, step: timedelta) -> int:
    calls = int((end - start) / step) + 1
    return calls * 10 * live_cost(sport)


def month_end(now: datetime) -> datetime:
    last = calendar.monthrange(now.year, now.month)[1]
    return now.replace(day=last, hour=23, minute=59, second=59, microsecond=0)


def next_delay(now: datetime, commences: list[datetime], credits_remaining: int | None, cost: int,
               resets_at: datetime | None = None) -> timedelta:
    """How long to wait before the next poll of one sport.

    Poll fast near kickoffs and slowly otherwise, never faster than three times the
    rate that would spend the remaining credits evenly before they reset, and wake
    a few minutes before each kickoff so the close is fresh.
    """
    upcoming = sorted(c for c in commences if c > now)
    soon = upcoming[0] - now if upcoming else None
    if soon is None:
        delay = timedelta(hours=6)
    elif soon < timedelta(hours=1):
        delay = timedelta(minutes=5)
    elif soon < timedelta(hours=3):
        delay = timedelta(minutes=10)
    elif soon < timedelta(hours=12):
        delay = timedelta(minutes=20)
    elif soon < timedelta(hours=48):
        delay = timedelta(hours=1)
    else:
        delay = timedelta(hours=3)

    if credits_remaining is not None and cost:
        polls_left = max(credits_remaining // cost, 1)
        even = (resets_at or month_end(now)) - now
        delay = max(delay, even / polls_left / 3)

    for kickoff in upcoming:
        wake = kickoff - timedelta(minutes=4)
        if now + timedelta(minutes=1) < wake < now + delay:
            return wake - now
    return delay
