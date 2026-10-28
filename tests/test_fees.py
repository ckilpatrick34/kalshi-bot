from kalshi_bot.fees import maker_fee, taker_fee


def test_taker_fee_matches_schedule():
    assert taker_fee(0.50, 100) == 1.75
    assert taker_fee(0.05, 100) == 0.34
    assert taker_fee(0.90, 100) == 0.63


def test_taker_fee_rounds_up_to_cent():
    assert taker_fee(0.05, 1) == 0.01


def test_maker_fee_zero_on_standard_series():
    assert maker_fee(0.50, 100) == 0.0
    assert maker_fee(0.50, 100, "quadratic_with_maker_fees") == 0.44
    assert maker_fee(0.50, 100, "quadratic_with_combo_maker_fees") == 0.88
