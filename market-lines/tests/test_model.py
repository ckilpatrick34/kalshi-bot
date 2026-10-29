import random
from datetime import datetime, timedelta, timezone

import pytest

from marketlines.feed import Event
from marketlines.lines import Timeline
from marketlines.model import cover_prob, fit, fit_mass, over_prob, walk_forward, win_prob
from marketlines.projections import collect, refit
from marketlines.results import Result, parse_neutral, parse_scoreboard
from marketlines.signals import model_edge
from marketlines.sports import SPORTS, use_fitted_tables
from marketlines.store import Store
from marketlines.teams import TeamMatcher, normalise

START = datetime(2025, 8, 30, 18, tzinfo=timezone.utc)
J = 1.9090909


def synthetic_season(seed=7, teams=40, weeks=14, noise=14.0, home_edge=3.0):
    """Round-robin-ish season from known team strengths, with noisy scores."""
    rng = random.Random(seed)
    names = [f"Team{i} Tigers" for i in range(teams)]
    off = {t: rng.gauss(0, 7) for t in names}
    dfn = {t: rng.gauss(0, 7) for t in names}
    results, gid = [], 0
    for week in range(weeks):
        order = names[:]
        rng.shuffle(order)
        for home, away in zip(order[::2], order[1::2]):
            neutral = rng.random() < 0.1
            edge = 0 if neutral else home_edge / 2
            hp = 28 + off[home] - dfn[away] + edge + rng.gauss(0, noise / 1.41)
            ap = 28 + off[away] - dfn[home] - edge + rng.gauss(0, noise / 1.41)
            gid += 1
            results.append(Result(str(gid), "ncaaf", START + timedelta(weeks=week), home, away,
                                  max(0, round(hp)), max(0, round(ap)), neutral))
    return results, off, dfn


def test_fit_recovers_strengths():
    results, off, dfn = synthetic_season()
    ratings = fit(results, "ncaaf", START + timedelta(weeks=20), half_life=10_000, ridge=1.0)
    true_power = {t: off[t] + dfn[t] for t in off}
    est = [ratings.power(t) for t in off]
    truth = [true_power[t] for t in off]
    # Correlation between fitted and true power should be strong.
    n = len(est)
    me, mt = sum(est) / n, sum(truth) / n
    cov = sum((e - me) * (t - mt) for e, t in zip(est, truth))
    corr = cov / (sum((e - me) ** 2 for e in est) * sum((t - mt) ** 2 for t in truth)) ** 0.5
    assert corr > 0.85
    assert 1.0 < ratings.home_edge < 5.0
    # Ridge centres the ratings, so the intercept absorbs the sample's average offense and defense.
    expected_avg = 28 + sum(off.values()) / len(off) - sum(dfn.values()) / len(dfn)
    assert ratings.league_avg == pytest.approx(expected_avg, abs=1.0)


def test_fit_uses_only_earlier_games():
    results, _, _ = synthetic_season()
    early = fit(results, "ncaaf", START + timedelta(weeks=2))
    assert sum(early.games.values()) == 2 * 2 * 20  # two weeks of 20 games, two teams each


def test_walk_forward_beats_guessing():
    results, _, _ = synthetic_season(weeks=14)
    check, scored = walk_forward(results, "ncaaf", START + timedelta(weeks=5), START + timedelta(weeks=14))
    assert check.games == len(scored) > 100
    assert check.winner_right > 0.6
    assert check.brier < 0.24
    assert 10 < check.rmse_margin < 20


def test_probabilities():
    assert win_prob(0, 15) == pytest.approx(0.5)
    assert win_prob(7, 15) > 0.65
    # Model says home by 7: home -6.5 covers a bit over half, -7 (push removed) is 50/50.
    assert cover_prob(7, -6.5, 15) > 0.5
    assert cover_prob(7, -7, 15) == pytest.approx(0.5)
    assert cover_prob(7, -10.5, 15) < 0.5
    assert over_prob(140, 135.5, 12) > 0.6
    assert over_prob(140, 140, 12) == pytest.approx(0.5)


def test_fit_mass_shrinks_toward_prior():
    table = fit_mass([3, 3, 3, 7], prior=lambda k: 0.1, strength=4)
    assert table[3] == pytest.approx((3 + 0.4) / 8)
    assert table[5] == pytest.approx(0.4 / 8)


def test_team_matcher():
    m = TeamMatcher(["Ole Miss Rebels", "Connecticut Huskies", "San José State Spartans", "Miami Hurricanes"])
    assert m.match("Mississippi Rebels") == "Ole Miss Rebels"
    assert m.match("UConn Huskies") == "Connecticut Huskies"
    assert m.match("San Jose State Spartans") == "San José State Spartans"
    assert m.match("Miami (FL) Hurricanes") == "Miami Hurricanes"
    assert m.match("Miami (OH) RedHawks") is None
    assert m.unmatched == {"Miami (OH) RedHawks"}
    assert normalise("Texas A&M Aggies") == "texas a and m aggies"


def espn_payload():
    def comp(gid, home, away, hs, as_, done, neutral=False):
        return {"id": gid, "date": "2026-10-25T23:30Z", "competitions": [{
            "date": "2026-10-25T23:30Z", "neutralSite": neutral,
            "status": {"type": {"completed": done}},
            "competitors": [
                {"homeAway": "home", "score": str(hs), "team": {"displayName": home}},
                {"homeAway": "away", "score": str(as_), "team": {"displayName": away}},
            ]}]}
    return {"events": [
        comp("1", "Ole Miss Rebels", "LSU Tigers", 31, 24, True),
        comp("2", "Alabama Crimson Tide", "Georgia Bulldogs", 0, 0, False, neutral=True),
    ]}


def test_parse_scoreboard():
    results = parse_scoreboard(espn_payload(), "ncaaf")
    assert len(results) == 1
    r = results[0]
    assert (r.home, r.away, r.margin, r.total, r.neutral) == ("Ole Miss Rebels", "LSU Tigers", 7, 55, False)
    assert [(h, a) for h, a, _ in parse_neutral(espn_payload())] == [("Alabama Crimson Tide", "Georgia Bulldogs")]


def test_model_edge_signal():
    t = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
    books = ["pinnacle", "draftkings", "fanduel", "bovada"]
    tl = Timeline([(t, b, -3.0, J, J) for b in books])
    # Model has home by 10: home -3 is well +EV, away +3 is not.
    sigs = model_edge(SPORTS["ncaaf"], "spreads", "e1", tl, t, (10.0, 55.0, 15.5, 15.0))
    assert [(s.kind, s.side_name, s.line) for s in sigs] == [("model", "home", -3.0)]
    assert sigs[0].fair > 0.6 and sigs[0].detail["model_margin"] == 10.0
    # Model agrees with the market: nothing.
    assert model_edge(SPORTS["ncaaf"], "spreads", "e1", tl, t, (3.0, 55.0, 15.5, 15.0)) == []
    assert model_edge(SPORTS["ncaaf"], "spreads", "e1", tl, t, None) == []


class FakeEspn:
    def __init__(self, results, neutral):
        self.results, self.neutral = results, neutral

    def days(self, sport, start, end):
        yield start, self.results, self.neutral, []


def test_refit_projects_feed_events(tmp_path):
    store = Store(str(tmp_path / "m.db"))
    original = dict(SPORTS)
    try:
        results, _, _ = synthetic_season(weeks=16)
        now = START + timedelta(weeks=16)
        kick = now + timedelta(days=2)
        collect(store, FakeEspn(results, [("Team1 Tigers", "Team2 Tigers", kick)]), "ncaaf", now.date(), now.date())
        store.upsert_events([
            Event("ev1", "ncaaf", kick, "Team1 Tigers", "Team2 Tigers"),
            Event("ev2", "ncaaf", kick, "Team3 Tigers", "Nobody State Owls"),
        ])
        out = refit(store, "ncaaf", now)
        assert out.projected == 1 and out.unmatched == {"Nobody State Owls"}
        assert out.check is not None and out.check.games >= 100
        margin, total, sd_margin, sd_total = store.projection("ev1")
        expected, _ = out.ratings.project("Team1 Tigers", "Team2 Tigers", neutral=True)
        assert margin == pytest.approx(expected)
        assert sd_margin == pytest.approx(out.check.rmse_margin)
        # 320 games is enough to refit the point tables, which then feed the detectors.
        assert out.tables and store.mass_tables()[("ncaaf", "margin")]
        use_fitted_tables(store.mass_tables())
        assert SPORTS["ncaaf"].margin_mass(7) == store.mass_tables()[("ncaaf", "margin")][7]
    finally:
        SPORTS.clear()
        SPORTS.update(original)


# Book lines and the market test

from marketlines.edges import market_test, price_slate  # noqa: E402
from marketlines.results import BookLine, parse_lines, summary_line  # noqa: E402


def test_parse_lines_from_scoreboard():
    payload = {"events": [{"id": "9", "date": "2026-10-31T16:00Z", "competitions": [{
        "neutralSite": True, "date": "2026-10-31T16:00Z",
        "competitors": [{"homeAway": "home", "team": {"displayName": "Navy Midshipmen"}},
                        {"homeAway": "away", "team": {"displayName": "Notre Dame Fighting Irish"}}],
        "odds": [{"provider": {"name": "DraftKings"}, "spread": 30.5, "overUnder": 56.5,
                  "homeTeamOdds": {"favorite": False}, "awayTeamOdds": {"favorite": True},
                  "pointSpread": {"home": {"open": {"line": "+28.5", "odds": "-110"}, "close": {"line": "+30.5", "odds": "-110"}},
                                  "away": {"close": {"line": "-30.5", "odds": "-110"}}},
                  "total": {"over": {"open": {"line": "o55.5"}, "close": {"line": "o56.5", "odds": "-112"}},
                            "under": {"close": {"line": "u56.5", "odds": "EVEN"}}},
                  "moneyline": {"home": {"close": {"odds": "+2000"}}, "away": {"close": {"odds": "-6500"}}}}],
    }]}]}
    (l,) = parse_lines(payload, "ncaaf")
    assert (l.home, l.spread, l.spread_open, l.total, l.total_open, l.neutral) == ("Navy Midshipmen", 30.5, 28.5, 56.5, 55.5, True)
    assert l.under_price == 2.0 and l.ml_home == 21.0
    assert l.spread_home_price == pytest.approx(1.909, abs=1e-3)


def test_summary_line_uses_favourite_for_sign():
    r = Result("5", "ncaaf", START, "Miami Hurricanes", "Stanford Cardinal", 40, 10, False)
    summary = {"pickcenter": [{"provider": {"name": "DraftKings"}, "spread": 20.5, "overUnder": 50.5,
                               "overOdds": -118, "underOdds": -102,
                               "homeTeamOdds": {"favorite": True, "spreadOdds": -112, "moneyLine": -2000},
                               "awayTeamOdds": {"favorite": False, "spreadOdds": -108, "moneyLine": 950}}]}
    l = summary_line(summary, r)
    assert l.spread == -20.5 and l.total == 50.5 and l.ml_away == 10.5
    assert summary_line({"pickcenter": []}, r) is None


def _lines_for(results, bias=0.0):
    """A 'market' that knows the truth with small noise: margin minus noise, shifted by bias."""
    rng = random.Random(3)
    out = []
    for r in results:
        market_margin = r.margin + rng.gauss(0, 4) + bias
        out.append(BookLine(r.game_id, r.sport, r.played, r.home, r.away, r.neutral, "Test",
                            None, -round(market_margin * 2) / 2, J, J, None, round(r.total + rng.gauss(0, 5)) + 0.5, J, J, None, None))
    return out


def test_market_test_trusts_a_sharper_market():
    results, _, _ = synthetic_season(weeks=16)
    test, _rows = market_test(results, _lines_for(results), "ncaaf",
                              START + timedelta(weeks=5), START + timedelta(weeks=16))
    # This market is far sharper than a scores-only model, so the blend leans on it.
    assert test.market_mae < test.model_mae
    assert test.weight < 0.3
    assert sum(b.bets for b in test.spread_buckets) == test.games


def test_price_slate_finds_no_edge_when_model_agrees():
    results, _, _ = synthetic_season(weeks=16)
    now = START + timedelta(weeks=16)
    ratings = fit(results, "ncaaf", now)
    margin, total = ratings.project("Team1 Tigers", "Team2 Tigers")
    agree = BookLine("u1", "ncaaf", now + timedelta(days=2), "Team1 Tigers", "Team2 Tigers", False, "Test",
                     None, -round(margin * 2) / 2 - 0.5, J, J, None, round(total) + 0.5, J, J, None, None)
    test, _ = market_test(results, _lines_for(results), "ncaaf", START + timedelta(weeks=5), now)
    plays = price_slate(ratings, test, [agree])
    assert plays and all(p.ev < 0.01 for p in plays)
