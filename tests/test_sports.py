from datetime import datetime, timedelta, timezone

import pytest

from kalshi_bot.fees import polymarket_us_taker_fee
from kalshi_bot.sports.config import LEAGUES, SportsConfig
from kalshi_bot.sports.devig import american_to_decimal, devig
from kalshi_bot.sports.edge import evaluate, kelly_fraction, maker_price
from kalshi_bot.sports.engine import Engine
from kalshi_bot.sports.ledger import Ledger
from kalshi_bot.sports.odds import Game, fair_from_event, winner_from_score
from kalshi_bot.sports.teams import match_pair, team_score
from kalshi_bot.sports.venues import Contract, Matched, kalshi_contracts, kalshi_event_date, polymarket_contracts

NOW = datetime(2026, 11, 1, 15, 0, tzinfo=timezone.utc)
KICKOFF = datetime(2026, 11, 1, 18, 0, tzinfo=timezone.utc)


def game(home_fair=0.60, commence=KICKOFF, gid="g1"):
    return Game(gid, "nfl", commence, "Kansas City Chiefs", "Las Vegas Raiders",
                {"Kansas City Chiefs": home_fair, "Las Vegas Raiders": 1 - home_fair}, ["pinnacle"])


def matched(bid, ask, fair=0.60, venue="kalshi", ask_size=1000.0, g=None):
    g = g or game(fair)
    c = Contract(venue, "KXNFLGAME-26NOV01LVKC-KC", "Kansas City", "yes", bid, ask, ask_size)
    return Matched(c, g, "Kansas City Chiefs")


# --- devig -------------------------------------------------------------------


def test_american_to_decimal():
    assert american_to_decimal(-150) == pytest.approx(1 + 100 / 150)
    assert american_to_decimal(130) == pytest.approx(2.3)


def test_devig_methods_sum_to_one_and_remove_margin():
    odds = [1.5, 2.7]  # implied 0.667 + 0.370 = 1.037
    for method in ("multiplicative", "power"):
        fair = devig(odds, method)
        assert sum(fair) == pytest.approx(1)
        assert fair[0] < 1 / 1.5 and fair[1] < 1 / 2.7


def test_power_devig_takes_more_from_the_longshot():
    odds = [1.2, 5.0]
    assert devig(odds, "power")[1] < devig(odds, "multiplicative")[1]


def test_fair_from_event_averages_sharp_books_and_ignores_others():
    event = {
        "id": "e1", "commence_time": "2026-11-01T18:00:00Z", "home_team": "A", "away_team": "B",
        "bookmakers": [
            {"key": "pinnacle", "markets": [{"key": "h2h", "outcomes": [{"name": "A", "price": 1.9}, {"name": "B", "price": 1.9}]}]},
            {"key": "draftkings", "markets": [{"key": "h2h", "outcomes": [{"name": "A", "price": 3.0}, {"name": "B", "price": 1.3}]}]},
        ],
    }
    g = fair_from_event(event, "nfl", ["pinnacle"])
    assert g.fair == {"A": pytest.approx(0.5), "B": pytest.approx(0.5)}
    assert g.books == ["pinnacle"]
    assert fair_from_event(event, "nfl", ["circa"]) is None


def test_winner_from_score():
    ev = {"completed": True, "scores": [{"name": "A", "score": "24"}, {"name": "B", "score": "17"}]}
    assert winner_from_score(ev) == "A"
    assert winner_from_score({**ev, "completed": False}) is None


# --- fees and team matching --------------------------------------------------


def test_polymarket_us_taker_fee():
    assert polymarket_us_taker_fee(0.50, 100) == 1.25


def test_team_names_match_across_venues():
    assert team_score("Kansas City", "Kansas City Chiefs") >= 0.6
    assert team_score("Chiefs", "Kansas City Chiefs") >= 0.6
    assert team_score("Los Angeles C", "Los Angeles Chargers") > team_score("Los Angeles C", "Los Angeles Rams")
    assert team_score("Red Sox", "Boston Red Sox") >= 0.9
    assert match_pair(("Las Vegas", "Kansas City"), ("Kansas City Chiefs", "Las Vegas Raiders")) == {
        "Las Vegas": "Las Vegas Raiders", "Kansas City": "Kansas City Chiefs"}


def test_ambiguous_pairing_is_skipped():
    assert match_pair(("Los Angeles", "Los Angeles"), ("Los Angeles Lakers", "Los Angeles Clippers")) is None
    assert match_pair(("Denver", "Miami"), ("Kansas City Chiefs", "Las Vegas Raiders")) is None


# --- venue parsing -------------------------------------------------------------


def test_kalshi_event_date():
    assert kalshi_event_date("KXNFLGAME-26NOV01LVKC").isoformat() == "2026-11-01"
    assert kalshi_event_date("NOPE") is None


def test_kalshi_contracts_match_by_team_and_date():
    markets = [
        {"ticker": "KXNFLGAME-26NOV01LVKC-KC", "event_ticker": "KXNFLGAME-26NOV01LVKC", "yes_sub_title": "Kansas City",
         "yes_bid_dollars": "0.56", "yes_ask_dollars": "0.58", "yes_ask_size_fp": "300"},
        {"ticker": "KXNFLGAME-26NOV01LVKC-LV", "event_ticker": "KXNFLGAME-26NOV01LVKC", "yes_sub_title": "Las Vegas",
         "yes_bid_dollars": "0.41", "yes_ask_dollars": "0.43"},
        # same teams a week later must not match this game
        {"ticker": "KXNFLGAME-26NOV08LVKC-KC", "event_ticker": "KXNFLGAME-26NOV08LVKC", "yes_sub_title": "Kansas City"},
        {"ticker": "KXNFLGAME-26NOV08LVKC-LV", "event_ticker": "KXNFLGAME-26NOV08LVKC", "yes_sub_title": "Las Vegas"},
    ]
    out = kalshi_contracts(markets, [game()], "quadratic", 1.0)
    assert {(m.contract.market_id, m.team) for m in out} == {
        ("KXNFLGAME-26NOV01LVKC-KC", "Kansas City Chiefs"), ("KXNFLGAME-26NOV01LVKC-LV", "Las Vegas Raiders")}
    kc = next(m for m in out if m.team == "Kansas City Chiefs")
    assert (kc.contract.bid, kc.contract.ask, kc.contract.ask_size, kc.fair) == (0.56, 0.58, 300.0, 0.60)


def test_polymarket_contracts_split_long_and_short():
    events = [{
        "startTime": "2026-11-01T18:00:00Z", "live": False, "ended": False,
        "teams": [{"id": 1, "name": "Chiefs"}, {"id": 2, "name": "Raiders"}],
        "markets": [{
            "slug": "nfl-lv-kc-2026-11-01", "sportsMarketType": "SPORTS_MARKET_TYPE_MONEYLINE",
            "marketSides": [{"teamId": 2, "long": False}, {"teamId": 1, "long": True}],
            "bestBidQuote": {"value": "0.57", "currency": "USD"}, "bestAskQuote": {"value": "0.59", "currency": "USD"},
        }],
    }]
    out = {m.team: m.contract for m in polymarket_contracts(events, [game()])}
    assert (out["Kansas City Chiefs"].side, out["Kansas City Chiefs"].bid, out["Kansas City Chiefs"].ask) == ("long", 0.57, 0.59)
    assert (out["Las Vegas Raiders"].side, out["Las Vegas Raiders"].bid, out["Las Vegas Raiders"].ask) == ("short", 0.41, 0.43)
    assert polymarket_contracts([{**events[0], "live": True}], [game()]) == []


# --- edge and sizing -----------------------------------------------------------


def test_kelly_fraction():
    assert kelly_fraction(0.60, 0.50) == pytest.approx(0.2)
    assert kelly_fraction(0.50, 0.55) == 0


def test_fairly_priced_contract_only_gets_a_bid_behind_the_book():
    d = evaluate(matched(0.59, 0.61, fair=0.60), SportsConfig(), 0)
    assert d.kind == "maker" and d.price == 0.58
    assert evaluate(matched(0.59, 0.61, fair=0.59), SportsConfig(), 0).price == 0.57


def test_taker_when_ask_beats_fair_after_fee():
    cfg = SportsConfig(bankroll=1000)
    d = evaluate(matched(0.53, 0.55, fair=0.60), cfg, 0)
    assert d.kind == "taker" and d.price == 0.55
    assert d.edge >= cfg.taker_min_edge
    # quarter Kelly of (0.60 - c) / (1 - c), capped at 3% of bankroll on one game
    assert d.price * d.contracts + d.fee <= 0.03 * 1000 + 1e-9


def test_taker_size_is_capped_by_book_depth():
    d = evaluate(matched(0.53, 0.55, fair=0.60, ask_size=7), SportsConfig(bankroll=10_000), 0)
    assert d.kind == "taker" and d.contracts == 7


def test_maker_bids_one_tick_over_the_bid_when_taking_is_too_thin():
    cfg = SportsConfig(bankroll=1000)
    d = evaluate(matched(0.55, 0.59, fair=0.60), cfg, 0)
    assert d.kind == "maker" and d.price == 0.56 and d.fee == 0
    assert d.edge >= cfg.maker_min_edge


def test_maker_price_never_gives_up_the_edge_or_crosses():
    cfg = SportsConfig()
    assert maker_price(matched(0.59, 0.61, fair=0.60), cfg) == 0.58  # caps at fair - min edge
    assert maker_price(matched(0.40, 0.45, fair=0.60), cfg) == 0.41
    assert maker_price(matched(0.55, 0.56, fair=0.60), cfg) == 0.55  # one tick under the ask at most


def test_game_exposure_cap_stops_new_orders():
    cfg = SportsConfig(bankroll=1000, max_game_pct=0.03)
    assert evaluate(matched(0.53, 0.55, fair=0.60), cfg, game_exposure=30) is None


def test_polymarket_maker_pays_no_fee():
    d = evaluate(matched(0.55, 0.59, fair=0.60, venue="polymarket"), SportsConfig(), 0)
    assert d.kind == "maker" and d.fee == 0


# --- engine and ledger -----------------------------------------------------------


class FakeOdds:
    def __init__(self, games):
        self.games = games
        self.score_events = []

    def fair_values(self, league, sport, books, method):
        return self.games if league == "nfl" else []

    def scores(self, sport):
        return self.score_events


class FakeVenue:
    def __init__(self, contracts):
        self.contracts_ = contracts

    def contracts(self, league, games):
        return self.contracts_ if league is LEAGUES["nfl"] else []


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def make_engine(contracts, games, clock):
    cfg = SportsConfig(leagues=["nfl"], venues=["kalshi"], bankroll=1000, ledger_path=":memory:")
    return Engine(cfg, FakeOdds(games), {"kalshi": FakeVenue(contracts)}, Ledger(":memory:"), now=clock)


def test_paper_cycle_rests_fills_records_clv_and_settles():
    clock = Clock(NOW)
    g = game(0.60)
    m = matched(0.55, 0.59, g=g)
    engine = make_engine([m], [g], clock)

    events = engine.cycle()
    assert any(e.startswith("maker paper kalshi") for e in events)
    [order] = engine.ledger.resting("paper")
    assert order["price"] == 0.56

    # nothing changes: the order stays, no duplicate is placed
    clock.t = NOW + timedelta(minutes=5)
    engine.cycle()
    assert len(engine.ledger.resting("paper")) == 1

    # a seller drops the ask to our price: paper fill
    m.contract.ask = 0.56
    clock.t = NOW + timedelta(minutes=10)
    events = engine.cycle()
    assert any(e.startswith("paper fill") for e in events)
    filled = engine.ledger.db.execute("SELECT price FROM orders WHERE status = 'filled'").fetchall()
    assert [r["price"] for r in filled] == [0.56]

    # sharp line moves to 0.62 before kickoff; after kickoff that becomes the closing line
    g.fair = {"Kansas City Chiefs": 0.62, "Las Vegas Raiders": 0.38}
    m.contract.bid, m.contract.ask = 0.61, 0.63
    clock.t = KICKOFF - timedelta(minutes=30)
    engine.cycle()
    clock.t = KICKOFF + timedelta(minutes=1)
    engine.cycle()
    row = engine.ledger.db.execute("SELECT * FROM orders WHERE status = 'filled'").fetchone()
    assert row["closing_fair"] == pytest.approx(0.62)
    assert row["clv"] == pytest.approx(0.62 - 0.56)

    engine.odds.score_events = [{"id": "g1", "completed": True, "home_team": "Kansas City Chiefs",
                                 "away_team": "Las Vegas Raiders", "scores": [
                                     {"name": "Kansas City Chiefs", "score": "27"}, {"name": "Las Vegas Raiders", "score": "20"}]}]
    engine.settle()
    report = engine.ledger.report("paper")
    n = row["contracts"]
    assert report["overall"]["pnl"] == pytest.approx(n * (1 - 0.56))
    assert report["overall"]["avg_clv"] == pytest.approx(0.06)


def test_resting_order_cancelled_when_fair_drops_and_before_start():
    clock = Clock(NOW)
    g = game(0.60)
    m = matched(0.55, 0.59, g=g)
    engine = make_engine([m], [g], clock)
    engine.cycle()
    g.fair = {"Kansas City Chiefs": 0.55, "Las Vegas Raiders": 0.45}
    events = engine.cycle()
    assert any("fair moved" in e for e in events)
    [order] = engine.ledger.resting("paper")
    assert order["price"] == 0.53  # re-quoted below the new fair value
    clock.t = KICKOFF - timedelta(minutes=2)
    events = engine.cycle()
    assert any("game starting" in e for e in events)
    assert engine.ledger.resting("paper") == []


def test_scan_places_nothing():
    clock = Clock(NOW)
    g = game(0.60)
    engine = make_engine([matched(0.53, 0.55, g=g)], [g], clock)
    events = engine.cycle(trade=False)
    assert len(events) == 1 and "taker" in events[0]
    assert engine.ledger.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_config_rejects_unknown_venue():
    with pytest.raises(ValueError):
        SportsConfig(venues=["fanduel"])
