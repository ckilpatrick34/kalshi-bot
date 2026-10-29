from datetime import datetime, timedelta, timezone

import pytest

from marketlines.clv import signal_clv
from marketlines.engine import backfill_cost, ingest, next_delay, settle
from marketlines.feed import parse_events
from marketlines.lines import Consensus, Line, Timeline, consensus, shift_prob, span_mass
from marketlines.prices import american_to_decimal, decimal_to_american, devig_pair
from marketlines.signals import detect, divergence, drift, key_numbers, rlm, steam
from marketlines.sports import SPORTS
from marketlines.store import Store

CFB = SPORTS["ncaaf"]
CBB = SPORTS["ncaab"]
T0 = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
BOOKS = ["pinnacle", "draftkings", "fanduel", "betmgm", "bovada"]
J = american_to_decimal(-110)


def rows(ts, lines):
    """lines: {book: (point, price_a, price_b)}"""
    return [(ts, book, *vals) for book, vals in lines.items()]


def spread_rows(ts, points, books=BOOKS):
    if not isinstance(points, dict):
        points = {b: points for b in books}
    return rows(ts, {b: (p, J, J) for b, p in points.items()})


# Prices and lines

def test_price_conversions():
    assert american_to_decimal(-110) == pytest.approx(1.9091, abs=1e-4)
    assert american_to_decimal(150) == 2.5
    assert decimal_to_american(2.5) == 150
    assert decimal_to_american(american_to_decimal(-110)) == -110
    assert devig_pair(J, J) == pytest.approx(0.5)
    fav = devig_pair(american_to_decimal(-200), american_to_decimal(170))
    assert 0.63 < fav < 0.66


def test_span_mass_counts_endpoints_as_half():
    mass = CFB.margin_mass
    assert span_mass(2.5, 3.5, mass) == pytest.approx(mass(3))
    assert span_mass(3, 3.5, mass) == pytest.approx(mass(3) / 2)
    assert span_mass(2.5, 2.5, mass) == 0


def test_shift_prob_spreads_and_totals():
    # Home -2.5 to -3.5 costs home the whole 3-point mass.
    assert shift_prob(CFB, "spreads", 0.5, -2.5, -3.5) == pytest.approx(0.5 - CFB.margin_mass(3))
    assert shift_prob(CFB, "spreads", 0.5, -3.5, -2.5) == pytest.approx(0.5 + CFB.margin_mass(3))
    # A higher total hurts the over.
    assert shift_prob(CBB, "totals", 0.5, 140.5, 142.5) < 0.5
    assert shift_prob(CBB, "h2h", 0.6, None, None) == 0.6


def test_consensus_prefers_sharp_book():
    lines = [Line("pinnacle", -4.0, J, J), Line("draftkings", -3.0, J, J), Line("fanduel", -3.0, J, J)]
    cons = consensus(CFB, "spreads", lines)
    assert cons.source == "pinnacle"
    assert cons.point == -3.0
    assert cons.fair_a > 0.5  # pinnacle has home -4, so home -3 covers more than half the time
    no_sharp = consensus(CFB, "spreads", lines[1:])
    assert no_sharp.source == "median" and no_sharp.fair_a == pytest.approx(0.5)


# Detectors

def test_steam_names_the_lagging_book():
    t1 = T0 + timedelta(minutes=10)
    tl = Timeline(spread_rows(T0, -3.0) + spread_rows(t1, {"pinnacle": -4.0, "draftkings": -4.0, "fanduel": -4.0, "betmgm": -4.0}))
    sigs = steam(CFB, "spreads", "e1", tl, T0, t1)
    assert len(sigs) == 1
    s = sigs[0]
    assert (s.kind, s.side_name, s.book, s.line) == ("steam", "home", "bovada", -3.0)
    assert s.detail["sharp_led"] and s.ev > 0


def test_no_steam_over_long_gaps_or_split_moves():
    t1 = T0 + timedelta(hours=2)
    tl = Timeline(spread_rows(T0, -3.0) + spread_rows(t1, -4.0))
    assert steam(CFB, "spreads", "e1", tl, T0, t1) == []
    t1 = T0 + timedelta(minutes=10)
    tl = Timeline(spread_rows(T0, -3.0) + spread_rows(t1, {"pinnacle": -4.0, "draftkings": -4.0, "fanduel": -4.0, "betmgm": -2.0}))
    assert steam(CFB, "spreads", "e1", tl, T0, t1) == []


def test_key_number_crossing():
    t1 = T0 + timedelta(minutes=20)
    tl = Timeline(spread_rows(T0, -2.5) + spread_rows(t1, {**{b: -3.5 for b in BOOKS[:4]}, "bovada": -2.5}))
    sigs = key_numbers(CFB, "spreads", "e1", tl, T0, t1)
    assert [(s.detail["key"], s.side_name, s.book) for s in sigs] == [(-3, "home", "bovada")]
    assert sigs[0].detail["through"]
    assert key_numbers(CBB, "spreads", "e1", tl, T0, t1) == []  # no key numbers in basketball


def test_drift_levels():
    t1 = T0 + timedelta(hours=6)
    tl = Timeline(spread_rows(T0, -3.0) + spread_rows(t1, -7.0))
    sigs = drift(CFB, "spreads", "e1", tl, t1)
    assert len(sigs) == 1 and sigs[0].side_name == "home"
    assert sigs[0].detail["level"] >= 2
    assert sigs[0].key.endswith(f":{sigs[0].detail['level']}")


def test_rlm_proxy_and_true_rlm():
    t1 = T0 + timedelta(hours=6)
    # Home opens +7 (the dog) and money takes it to +4.
    tl = Timeline(spread_rows(T0, 7.0) + spread_rows(t1, 4.0))
    proxy = rlm(CFB, "spreads", "e1", tl, t1, None)
    assert [(s.kind, s.side_name) for s in proxy] == [("rlm_proxy", "home")]
    # With splits: 70% of tickets on away and the line still moves to home = true RLM.
    true = rlm(CFB, "spreads", "e1", tl, t1, 0.30)
    assert [(s.kind, s.side_name) for s in true] == [("rlm", "home")]
    # Tickets on the same side as the move is not RLM.
    assert rlm(CFB, "spreads", "e1", tl, t1, 0.70) == []
    # A move toward the favourite is not a proxy signal.
    tl = Timeline(spread_rows(T0, 4.0) + spread_rows(t1, 7.0))
    assert rlm(CFB, "spreads", "e1", tl, t1, None) == []


def test_divergence_flags_the_off_market_book():
    tl = Timeline(spread_rows(T0, {"pinnacle": -5.0, "draftkings": -5.0, "fanduel": -5.0, "betmgm": -5.0, "bovada": -3.0}))
    sigs = divergence(CFB, "spreads", "e1", tl, T0)
    assert [(s.book, s.side_name, s.line) for s in sigs] == [("bovada", "home", -3.0)]
    assert sigs[0].ev > CFB.divergence_ev


def test_detect_needs_enough_books():
    tl = Timeline(spread_rows(T0, -3.0, books=BOOKS[:2]))
    assert detect(CFB, "spreads", "e1", tl, None, T0) == []


# CLV

def test_clv_signs():
    close = Consensus(-4.0, 0.5, "pinnacle", 5)
    # Took home -3 at -110 and it closed -4: beat the close by a point.
    home = signal_clv(CFB, "spreads", "a", -3.0, J, -3.0, 0.5, close)
    assert home.clv_points == 1.0 and home.clv_ev > 0 and home.clv_move > 0
    away = signal_clv(CFB, "spreads", "b", -3.0, J, -3.0, 0.5, close)
    assert away.clv_points == -1.0 and away.clv_ev < 0
    over = signal_clv(CBB, "totals", "a", 140.5, J, 140.5, 0.5, Consensus(143.5, 0.5, "pinnacle", 5))
    assert over.clv_points == 3.0 and over.clv_ev > 0
    ml = signal_clv(CFB, "h2h", "a", None, 2.2, None, 0.45, Consensus(None, 0.5, "pinnacle", 5))
    assert ml.clv_points is None and ml.clv_ev == pytest.approx(0.1) and ml.clv_move == pytest.approx(0.05)


# End to end through the store

def payload(home_point, commence, books=BOOKS, pulled=()):
    def odds(home, away, point_home, total):
        markets = [
            {"key": "h2h", "outcomes": [{"name": home, "price": 1.6}, {"name": away, "price": 2.45}]},
            {"key": "spreads", "outcomes": [{"name": home, "price": J, "point": point_home},
                                            {"name": away, "price": J, "point": -point_home}]},
            {"key": "totals", "outcomes": [{"name": "Over", "price": J, "point": total},
                                           {"name": "Under", "price": J, "point": total}]},
        ]
        return markets

    return [{
        "id": "g1", "sport_key": "americanfootball_ncaaf", "commence_time": commence.isoformat().replace("+00:00", "Z"),
        "home_team": "Ole Miss", "away_team": "LSU",
        "bookmakers": [{"key": b, "last_update": "2026-11-07T12:00:00Z",
                        "markets": odds("Ole Miss", "LSU", home_point if not isinstance(home_point, dict) else home_point[b], 55.5)}
                       for b in books if b not in pulled],
    }]


def test_parse_events():
    kick = T0 + timedelta(hours=8)
    events, quotes = parse_events(payload(-3.0, kick), "ncaaf")
    assert events[0].home == "Ole Miss" and events[0].commence == kick
    spreads = [q for q in quotes if q.market == "spreads"]
    assert len(spreads) == 5 and spreads[0].point == -3.0
    assert {q.market for q in quotes} == {"h2h", "spreads", "totals"}


def test_pipeline_end_to_end(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    kick = T0 + timedelta(hours=8)

    def snap(ts, points, pulled=()):
        events, quotes = parse_events(payload(points, kick, pulled=pulled), "ncaaf")
        return ingest(store, CFB, events, quotes, ts)

    assert snap(T0, -2.5) == []
    t1 = T0 + timedelta(minutes=10)
    new = snap(t1, {"pinnacle": -3.5, "draftkings": -3.5, "fanduel": -3.5, "betmgm": -3.5, "bovada": -2.5})
    kinds = {s.kind for s in new}
    assert {"steam", "key"} <= kinds
    # Same lines again: nothing changed, nothing new.
    assert snap(t1 + timedelta(minutes=10), {"pinnacle": -3.5, "draftkings": -3.5, "fanduel": -3.5, "betmgm": -3.5, "bovada": -2.5}) == []
    # Bovada pulls the market, then the line closes at -4.5 everywhere else.
    snap(kick - timedelta(minutes=5), -4.5, pulled=("bovada",))
    bovada = [r for r in store.timeline_rows("g1", "spreads") if r[1] == "bovada"]
    assert bovada[-1][3] is None
    # In-game snapshot is ignored.
    snap(kick + timedelta(minutes=30), -10.0)

    closes, scored = settle(store, kick + timedelta(hours=1))
    assert closes == 3 and scored >= 2
    close = store.query("SELECT point, source FROM closes WHERE event_id = 'g1' AND market = 'spreads'")[0]
    assert close == (-4.5, "pinnacle")
    steam_row = store.query("SELECT book, line, clv_points, clv_ev FROM signals WHERE kind = 'steam'")[0]
    assert steam_row[:3] == ("bovada", -2.5, 2.0) and steam_row[3] > 0
    assert all(r[3] is not None for r in store.ledger(T0 - timedelta(days=1)))


def test_next_delay():
    kick = T0 + timedelta(minutes=30)
    assert next_delay(T0, [kick], None, 3) == timedelta(minutes=5)
    assert next_delay(T0, [T0 + timedelta(days=5)], None, 3) == timedelta(hours=3)
    # Wakes just before a kickoff that falls inside the normal wait.
    assert next_delay(T0, [T0 + timedelta(hours=20), T0 + timedelta(hours=2, minutes=30)], None, 3) == timedelta(minutes=10)
    assert next_delay(T0, [T0 + timedelta(minutes=50)], None, 3) == timedelta(minutes=5)
    assert next_delay(T0, [T0 + timedelta(hours=13)], None, 3) == timedelta(hours=1)
    # A thin credit balance stretches the wait, but the pre-kickoff wake for the close still happens.
    far = T0 + timedelta(hours=20)
    assert next_delay(T0, [far], 30, 3, resets_at=T0 + timedelta(days=10)) == timedelta(hours=8)
    assert next_delay(T0, [kick], 30, 3, resets_at=T0 + timedelta(days=10)) == timedelta(minutes=26)


def test_backfill_cost():
    days = backfill_cost(CFB, T0, T0 + timedelta(days=1), timedelta(hours=3))
    assert days == 9 * 10 * 3
