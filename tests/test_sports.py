from datetime import datetime, timedelta, timezone

import pytest

from kalshi_bot.fees import polymarket_us_taker_fee
from kalshi_bot.sports.config import LEAGUES, SportsConfig
from kalshi_bot.sports.devig import american_to_decimal, devig
from kalshi_bot.sports.model import PARAMS, Elo, EloParams, backtest, margin_multiplier
from kalshi_bot.sports.odds import Game, fair_from_event
from kalshi_bot.sports.plays import model_games, plays_for
from kalshi_bot.sports.results import GameResult, parse_scoreboard, parse_time, read_csv, write_csv
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
    out = kalshi_contracts(markets, [game()])
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


# --- results -------------------------------------------------------------------


def test_parse_espn_scoreboard():
    data = {"events": [
        {"id": "401", "date": "2025-10-26T17:00Z", "status": {"type": {"name": "STATUS_FINAL", "state": "post"}},
         "competitions": [{"neutralSite": False, "status": {"type": {"state": "post", "completed": True}},
                           "competitors": [
                               {"homeAway": "home", "score": "10", "team": {"displayName": "Atlanta Falcons"}},
                               {"homeAway": "away", "score": "34", "team": {"displayName": "Miami Dolphins"}}]}]},
        {"id": "402", "date": "2026-11-01T18:00Z", "status": {"type": {"name": "STATUS_SCHEDULED", "state": "pre"}},
         "competitions": [{"neutralSite": True, "status": {"type": {"state": "pre"}}, "competitors": [
             {"homeAway": "home", "score": "0", "team": {"displayName": "Kansas City Chiefs"}},
             {"homeAway": "away", "score": "0", "team": {"displayName": "Las Vegas Raiders"}}]}]},
    ]}
    done, upcoming = parse_scoreboard(data, "nfl")
    assert (done.home, done.away, done.home_score, done.away_score, done.final) == (
        "Atlanta Falcons", "Miami Dolphins", 10.0, 34.0, True)
    assert done.start_time == datetime(2025, 10, 26, 17, 0, tzinfo=timezone.utc)
    assert upcoming.state == "pre" and upcoming.neutral and upcoming.home_score is None and not upcoming.final


def test_results_csv_round_trip(tmp_path):
    g = GameResult("1", "nfl", "2025-10-26T17:00:00+00:00", "A", "B", False, "post", 10.0, 34.0)
    write_csv(tmp_path / "x.csv", [g])
    assert read_csv(tmp_path / "x.csv") == [g]


# --- model ------------------------------------------------------------------------


def result(day, home, away, hs, as_, league="nfl"):
    start = (datetime(2025, 9, 1, 18, tzinfo=timezone.utc) + timedelta(days=day)).isoformat()
    return GameResult(f"{day}{home}{away}", league, start, home, away, False, "post", hs, as_)


def test_elo_home_advantage_and_updates():
    elo = Elo(PARAMS["nfl"])
    assert elo.predict("A", "B") == pytest.approx(1 / (1 + 10 ** (-48 / 400)))
    assert elo.predict("A", "B", neutral=True) == pytest.approx(0.5)
    p = elo.update(result(0, "A", "B", 30, 10))
    assert p > 0.5
    assert elo.ratings["A"] > 1500 > elo.ratings["B"]
    assert elo.ratings["A"] + elo.ratings["B"] == pytest.approx(3000)


def test_bigger_wins_move_ratings_more_but_with_diminishing_returns():
    assert margin_multiplier(21, 0) > margin_multiplier(3, 0)
    assert margin_multiplier(21, 0) - margin_multiplier(14, 0) < margin_multiplier(14, 0) - margin_multiplier(7, 0)
    assert margin_multiplier(14, 200) < margin_multiplier(14, 0)  # expected blowouts count less


def test_ratings_regress_toward_average_in_the_offseason():
    elo = Elo(EloParams(k=20, home_advantage=0, season_revert=0.5))
    for d in range(10):
        elo.update(result(d * 7, "A", "B", 30, 10))
    gap_now = elo.rating("A") - 1500
    next_season = elo.last_played["A"] + timedelta(days=200)
    assert elo.rating("A", next_season) - 1500 == pytest.approx(gap_now * 0.5)


def test_strong_team_becomes_favourite_and_backtest_scores_it():
    games = []
    for d in range(60):
        # A beats everyone, B loses to everyone, C and D split
        games += [result(d * 3, "A", "C", 28, 14), result(d * 3 + 1, "D", "B", 21, 7), result(d * 3 + 2, "C", "D", 17, 14 + (d % 2) * 6)]
    elo = Elo(PARAMS["nfl"]).fit(games)
    assert elo.predict("B", "A") < 0.15
    r = backtest(games, PARAMS["nfl"], parse_time(games[60].start))
    assert r["games"] == 120
    assert r["accuracy"] > 0.6
    assert r["log_loss"] < r["baseline_log_loss"]


# --- plays ------------------------------------------------------------------------


def trained_elo():
    games = [result(d, "Kansas City Chiefs" if d % 2 else "X", "Las Vegas Raiders" if d % 2 else "Y", 27, 13) for d in range(20)]
    return Elo(PARAMS["nfl"]).fit(games)


def test_model_games_and_plays_rank_by_ev():
    elo = trained_elo()
    upcoming = [GameResult("g1", "nfl", KICKOFF.isoformat(), "Kansas City Chiefs", "Las Vegas Raiders", False, "pre")]
    [g] = model_games(upcoming, elo)
    p_kc = g.fair["Kansas City Chiefs"]
    assert p_kc > 0.7 and g.fair["Las Vegas Raiders"] == pytest.approx(1 - p_kc)

    kc = Matched(Contract("kalshi", "KC", "Kansas City", "yes", 0.55, 0.57), g, "Kansas City Chiefs")
    lv = Matched(Contract("polymarket", "s", "Raiders", "short", 0.40, 0.42), g, "Las Vegas Raiders")
    no_ask = Matched(Contract("kalshi", "LV", "Las Vegas", "yes", 0.40, None), g, "Las Vegas Raiders")
    plays = plays_for([lv, kc, no_ask], elo, SportsConfig(), {("Kansas City Chiefs", "Las Vegas Raiders", "Kansas City Chiefs"): 0.6})
    assert [p.team for p in plays] == ["Kansas City Chiefs", "Las Vegas Raiders"]
    best = plays[0]
    assert best.fee == pytest.approx(0.07 * 0.57 * 0.43, abs=0.001)
    assert best.ev == pytest.approx(p_kc / (0.57 + best.fee) - 1)
    assert best.sharp_p == 0.6 and plays[1].sharp_p is None
    assert best.flagged(SportsConfig()) and not plays[1].flagged(SportsConfig())


def test_stake_is_quarter_kelly_capped_per_game():
    elo = trained_elo()
    upcoming = [GameResult("g1", "nfl", KICKOFF.isoformat(), "Kansas City Chiefs", "Las Vegas Raiders", False, "pre")]
    [g] = model_games(upcoming, elo)
    m = Matched(Contract("polymarket", "s", "Chiefs", "long", 0.30, 0.32), g, "Kansas City Chiefs")
    [play] = plays_for([m], elo, SportsConfig())
    cfg = SportsConfig(bankroll=1000, max_game_pct=0.03)
    assert play.stake(cfg) == 30.0
    cfg = SportsConfig(bankroll=1000, max_game_pct=1.0)
    kelly = play.edge / (1 - play.cost)
    assert play.stake(cfg) == pytest.approx(0.25 * kelly * 1000, abs=0.01)


def test_teams_without_history_are_marked_thin_and_not_flagged():
    elo = Elo(PARAMS["nfl"])
    upcoming = [GameResult("g1", "nfl", KICKOFF.isoformat(), "Kansas City Chiefs", "Las Vegas Raiders", False, "pre")]
    [g] = model_games(upcoming, elo)
    m = Matched(Contract("kalshi", "KC", "Kansas City", "yes", 0.20, 0.22), g, "Kansas City Chiefs")
    [play] = plays_for([m], elo, SportsConfig())
    assert play.thin_data and play.ev > 1 and not play.flagged(SportsConfig())


def test_config_rejects_unknown_venue():
    with pytest.raises(ValueError):
        SportsConfig(venues=["fanduel"])
