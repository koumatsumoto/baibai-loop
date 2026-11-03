from baibai_engine.machine_analysis.screening.policies.economic_path_inputs import (
    _provider_capital_crosscheck,
)


def test_provider_comparison_is_gross_and_does_not_treat_difference_as_truth():
    result = _provider_capital_crosscheck(
        {"price_date": "2026-11-02", "close_yen": 100.0, "market_cap_million_yen": 250.0},
        {"shares_outstanding": 2_000_000.0, "shares_ex_treasury": 1_800_000.0},
        expected_price_date="2026-11-02",
    )
    assert result["gross_market_cap_from_issued_yen"] == 200_000_000
    assert result["provider_to_reported_gross_ratio"] == 1.25
    assert result["interpretation"] == "comparison_only_provider_may_be_stale_or_on_another_basis"


def test_missing_or_wrong_price_day_does_not_produce_a_ratio():
    provider = {"price_date": "2026-10-30", "close_yen": 100.0, "market_cap_million_yen": 250.0}
    financial = {"shares_outstanding": 2_000_000.0}
    assert (
        _provider_capital_crosscheck(provider, financial, expected_price_date="2026-11-02")[
            "provider_to_reported_gross_ratio"
        ]
        is None
    )
    assert (
        _provider_capital_crosscheck({}, financial, expected_price_date="2026-11-02")[
            "provider_to_reported_gross_ratio"
        ]
        is None
    )
    assert (
        _provider_capital_crosscheck(provider, {}, expected_price_date="2026-10-30")[
            "provider_to_reported_gross_ratio"
        ]
        is None
    )
