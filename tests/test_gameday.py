from datetime import date

import pytest

from kalshi_bot.fees import taker_fee
from kalshi_bot.gameday import (
    Settings, american_to_prob, build_plays, devig_power, kalshi_games, match_games, next_game_day, normalize,
    parse_scoreboard, read_ledger, record, settle, size_plays,
)


def espn_event(id, date, home, away, home_ml, away_ml, home_abbr="H", away_abbr="A", state="pre"):
    def team(name, abbr):
        return {"location": name, "shortDisplayName": name, "displayName": name + " Mascots", "abbreviation": abbr}

    return {
        "id": id, "date": date,
        "competitions": [{
            "status": {"type": {"state": state}},
            "neutralSite": False,
            "competitors": [{"homeAway": "home", "team": team(home, home_abbr)},
                            {"homeAway": "away", "team": team(away, away_abbr)}],
            "odds": [{"provider": {"name": "DraftKings"}, "details": f"{home_abbr} -3",
                      "moneyline": {"home": {"close": {"odds": home_ml}}, "away": {"close": {"odds": away_ml}}}}],
        }],
    }


def kalshi_market(ticker, team, yes_ask, no_ask, when="2026-10-31T19:30:00Z", volume="5000.00"):
    return {
        "ticker": ticker, "event_ticker": ticker.rsplit("-", 1)[0], "title": f"{team} wins", "yes_sub_title": team,
        "yes_bid_dollars": f"{yes_ask - 0.01:.4f}", "yes_ask_dollars": f"{yes_ask:.4f}", "no_ask_dollars": f"{no_ask:.4f}",
        "yes_ask_size_fp": "500.00", "no_ask_size_fp": "500.00", "volume_fp": volume,
        "occurrence_datetime": when, "close_time": "2026-11-02T00:00:00Z",
    }


def test_american_odds():
    assert american_to_prob("-150") == pytest.approx(0.6)
    assert american_to_prob("+150") == pytest.approx(0.4)
    assert american_to_prob("EVEN") == 0.5
    assert american_to_prob("OFF") is None


def test_devig_sums_to_one_and_shades_longshot():
    raw = [american_to_prob("-300"), american_to_prob("+240")]
    fair = devig_power(raw)
    assert sum(fair) == pytest.approx(1)
    # the longshot loses a bigger share of its implied probability than the favourite
    assert fair[1] / raw[1] < fair[0] / raw[0]


def test_normalize_matches_kalshi_and_espn_spellings():
    assert normalize("Ohio St.") == normalize("Ohio State")
    assert normalize("San José State") == "san jose state"
    assert normalize("St. Thomas") == "st thomas"


def test_parse_skips_started_games_and_devigs():
    payload = {"events": [espn_event("1", "2026-10-31T19:30Z", "Penn State", "Purdue", "-500", "+380"),
                          espn_event("2", "2026-10-31T19:30Z", "Iowa", "Wisconsin", "-150", "+130", state="in")]}
    games = parse_scoreboard(payload)
    assert [g.id for g in games] == ["1"]
    assert sum(games[0].fair.values()) == pytest.approx(1)
    assert games[0].fair["home"] > 0.8


def test_match_uses_teams_even_with_placeholder_time():
    book = parse_scoreboard({"events": [espn_event("1", "2026-10-31T20:15Z", "Penn State", "Purdue", "-500", "+380",
                                                   "PSU", "PUR")]})
    markets = [kalshi_market("KXNCAAFGAME-26OCT30PURPSU-PSU", "Penn St.", 0.80, 0.21, when="2026-10-31T03:00:00Z"),
               kalshi_market("KXNCAAFGAME-26OCT30PURPSU-PUR", "Purdue", 0.21, 0.80, when="2026-10-31T03:00:00Z")]
    pairs = list(match_games(kalshi_games(markets, date(2026, 10, 31)), book))
    assert len(pairs) == 1
    game, sides = pairs[0]
    assert sides["home"].team == "Penn St." and sides["away"].team == "Purdue"


def test_flags_underpriced_team_and_respects_caps():
    # Book says Purdue is a 30% shot; Kalshi sells it at 20c.
    book = parse_scoreboard({"events": [espn_event("1", "2026-10-31T20:15Z", "Penn State", "Purdue", "-245", "+200",
                                                   "PSU", "PUR")]})
    markets = [kalshi_market("KXNCAAFGAME-26OCT31PURPSU-PSU", "Penn St.", 0.81, 0.20),
               kalshi_market("KXNCAAFGAME-26OCT31PURPSU-PUR", "Purdue", 0.21, 0.80)]
    s = Settings(max_bet=5, day_cap=50)
    plays = size_plays(build_plays(match_games(kalshi_games(markets, date(2026, 10, 31)), book), s), s)
    flagged = [p for p in plays if p.flagged]
    assert len(flagged) == 1
    p = flagged[0]
    assert p.team == "Purdue"
    # NO on Penn State at 20c is cheaper than YES on Purdue at 21c
    assert (p.side, p.ticker, p.ask) == ("no", "KXNCAAFGAME-26OCT31PURPSU-PSU", 0.20)
    assert p.stake <= 5
    assert p.limit_price < p.fair


def test_thin_and_extreme_markets_are_not_flagged():
    book = parse_scoreboard({"events": [espn_event("1", "2026-10-31T20:15Z", "Penn State", "Purdue", "-245", "+200")]})
    markets = [kalshi_market("KXNCAAFGAME-26OCT31PURPSU-PSU", "Penn St.", 0.81, 0.20, volume="10.00"),
               kalshi_market("KXNCAAFGAME-26OCT31PURPSU-PUR", "Purdue", 0.21, 0.80, volume="10.00")]
    s = Settings()
    plays = size_plays(build_plays(match_games(kalshi_games(markets, date(2026, 10, 31)), book), s), s)
    assert not any(p.flagged for p in plays)
    assert all("thin market" in p.notes for p in plays)


def test_paper_ledger_records_once_and_settles(tmp_path):
    book = parse_scoreboard({"events": [espn_event("1", "2026-10-31T20:15Z", "Penn State", "Purdue", "-245", "+200")]})
    markets = [kalshi_market("KXNCAAFGAME-26OCT31PURPSU-PSU", "Penn St.", 0.81, 0.20),
               kalshi_market("KXNCAAFGAME-26OCT31PURPSU-PUR", "Purdue", 0.21, 0.80)]
    s = Settings()
    plays = size_plays(build_plays(match_games(kalshi_games(markets, date(2026, 10, 31)), book), s), s)
    ledger = tmp_path / "gameday.jsonl"
    assert record(ledger, plays) == 1
    assert record(ledger, plays) == 0  # already open
    bet = read_ledger(ledger)[0]
    # NO on Penn State wins when Penn State loses
    settled = settle(ledger, lambda ticker: "no")
    assert settled[0]["status"] == "won"
    assert settled[0]["pnl"] == pytest.approx(bet["contracts"] * 0.80 - taker_fee(0.20, bet["contracts"]))


def test_next_game_day():
    assert next_game_day(date(2026, 10, 29)) == date(2026, 10, 31)  # Thursday
    assert next_game_day(date(2026, 10, 31)) == date(2026, 10, 31)  # Saturday
