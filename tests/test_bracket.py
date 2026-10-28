from kalshi_bot.strategies.bracket import Quote, scan_event


def test_finds_cheap_yes_ladder():
    quotes = [Quote(t, 0.28, 0.30, 50, 50) for t in "ABC"]
    opps = scan_event("E", quotes, max_contracts=10)
    assert [o.direction for o in opps] == ["buy_all_yes"]
    assert opps[0].profit > 0


def test_fees_kill_thin_arbitrage():
    quotes = [Quote(t, 0.48, 0.495, 50, 50) for t in "AB"]
    assert scan_event("E", quotes, max_contracts=10) == []


def test_finds_rich_bid_ladder():
    quotes = [Quote(t, 0.40, 0.42, 20, 20) for t in "ABC"]
    opps = scan_event("E", quotes, max_contracts=10)
    assert [o.direction for o in opps] == ["buy_all_no"]
    assert opps[0].profit > 0
