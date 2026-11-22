"""Pure TradingView forecast normalization and response accounting."""

import copy
import sqlite3
from contextlib import closing

import pytest
from tests.helpers.tradingview import NOW, payload

from baibai_engine.machine_analysis.observations.external_assessments.tradingview.observations import (
    FetchError,
    normalize_batch,
)


@pytest.mark.parametrize("forecast", [None, 0, -5, 2**63, -(2**64)])
def test_representable_forecasts_remain_storable_and_are_not_unresolved(forecast) -> None:
    data = payload(["TSE:1000"])
    data["data"]["TSE:1000"]["earnings_per_share_forecast_next_fy"] = forecast
    result = normalize_batch(data, ["TSE:1000"], NOW)
    assert result[0]["fetch_status"] == "ok"
    assert result[0]["recommendation_total"] is None
    value = result[0]["eps_forecast_next_fy"]
    assert value == forecast
    with closing(sqlite3.connect(":memory:")) as db:
        assert db.execute("SELECT CAST(? AS REAL)", (value,)).fetchone() == (forecast,)


@pytest.mark.parametrize(
    "case", ["unknown", "overlap", "duplicate_missing", "count", "missing_field", "nan"]
)
def test_response_accounting_refuses_invalid_payload(case: str) -> None:
    symbols = ["TSE:1000", "TSE:1001"]
    p = copy.deepcopy(payload(symbols, missing=[symbols[1]]))
    if case == "unknown":
        p["data"]["NAG:1000"] = p["data"].pop(symbols[0])
    elif case == "overlap":
        p["missing"][0]["symbol"] = symbols[0]
    elif case == "duplicate_missing":
        p["missing"].append(p["missing"][0])
    elif case == "count":
        p["count"] = 99
    elif case == "missing_field":
        p["data"][symbols[0]].pop("close")
    else:
        p["data"][symbols[0]]["close"] = float("nan")
    with pytest.raises(FetchError):
        normalize_batch(p, symbols, NOW)


@pytest.mark.parametrize(
    ("provider_field", "field", "value", "reason"),
    [
        ("close", "quote_close", float("nan"), "invalid_numeric"),
        ("close", "quote_close", 10**400, "invalid_numeric"),
        ("recommendation_total", "recommendation_total", 2**63, "invalid_analyst_count"),
        ("recommendation_total", "recommendation_total", -1, "invalid_analyst_count"),
        ("currency", "quote_currency", 42, "invalid_text_type"),
    ],
)
def test_payload_diagnostics_use_canonical_field(provider_field, field, value, reason):
    from baibai_engine.machine_analysis.observations.external_assessments.tradingview.observations import (
        ProviderPayloadError,
    )

    data = payload(["TSE:1000"])
    data["data"]["TSE:1000"][provider_field] = value
    with pytest.raises(ProviderPayloadError) as caught:
        normalize_batch(data, ["TSE:1000"], NOW)
    assert (caught.value.reason, caught.value.field) == (reason, field)


def test_missing_fields_uses_first_requested_canonical_field():
    from baibai_engine.machine_analysis.observations.external_assessments.tradingview.observations import (
        ProviderPayloadError,
    )

    data = payload(["TSE:1000"])
    del data["data"]["TSE:1000"]["earnings_per_share_forecast_next_fy"]
    del data["data"]["TSE:1000"]["close"]
    with pytest.raises(ProviderPayloadError) as caught:
        normalize_batch(data, ["TSE:1000"], NOW)
    assert caught.value.field == "eps_forecast_next_fy"


@pytest.mark.parametrize(
    ("other", "primary"),
    [
        ("same_shape", True),
        ("extra_key", False),
        ("missing_quote_key", False),
        ("one_forecast_is_null", False),
        ("full", False),
        ("non_object", False),
        ("explicit_missing", False),
    ],
)
def test_quote_only_diagnostic_requires_the_entire_exact_shape(other, primary):
    from baibai_engine.machine_analysis.observations.external_assessments.tradingview.observations import (
        ProviderPayloadError,
    )
    from baibai_jobs.observability.tradingview import failure_line

    symbols = ["TSE:SYNTH-PRIVATE-A", "TSE:SYNTH-PRIVATE-B"]
    response = payload(symbols, missing=symbols[1:] if other == "explicit_missing" else [])
    rows = response["data"]
    rows[symbols[0]] = {"close": 0, "currency": "PRIVATE"}
    if other == "same_shape":
        rows[symbols[1]] = {"close": None, "currency": None}
    elif other == "extra_key":
        rows[symbols[1]] = {**rows[symbols[0]], "PRIVATE-unknown": "PRIVATE"}
    elif other == "missing_quote_key":
        rows[symbols[1]] = {"close": 0}
    elif other == "one_forecast_is_null":
        rows[symbols[1]] = {**rows[symbols[0]], "AnalystRating": None}
    elif other == "non_object":
        rows[symbols[1]] = []
    with pytest.raises(ProviderPayloadError) as caught:
        normalize_batch(response, symbols, NOW)
    error = caught.value
    assert (error.reason, error.field) == (
        ("batch_quote_fields_only", None)
        if primary
        else ("row_missing_requested_fields", "analyst_rating")
    )
    assert "category=provider_response" in failure_line(error)
    assert "PRIVATE" not in failure_line(error)


@pytest.mark.parametrize("raw", [None, [], "private-body", 42])
def test_non_dict_row_does_not_blame_a_field(raw):
    from baibai_engine.machine_analysis.observations.external_assessments.tradingview.observations import (
        ProviderPayloadError,
    )
    from baibai_jobs.observability.tradingview import failure_line

    data = payload(["TSE:1000"])
    data["data"]["TSE:1000"] = raw
    with pytest.raises(ProviderPayloadError) as caught:
        normalize_batch(data, ["TSE:1000"], NOW)
    assert caught.value.reason == "row_missing_requested_fields"
    assert caught.value.field is None
    assert "validation_field=-" in failure_line(caught.value)
    assert "private-body" not in failure_line(caught.value)


@pytest.mark.parametrize("batch_size", [1, 2])
def test_explicit_missing_receipt_does_not_depend_on_batch_composition(batch_size):
    symbols = ["TSE:1000", "TSE:1001"]
    rows = []
    for offset in range(0, len(symbols), batch_size):
        chunk = symbols[offset : offset + batch_size]
        response = payload(chunk, missing=[s for s in chunk if s == symbols[1]])
        rows.extend(normalize_batch(response, chunk, NOW))
    assert rows == normalize_batch(payload(symbols, missing=[symbols[1]]), symbols, NOW)
    assert rows[1] == {
        "ticker": "1001",
        "provider_symbol": "TSE:1001",
        "fetched_at_utc": NOW.isoformat(),
        "fetch_status": "unresolved",
        "analyst_rating": None,
        "recommendation_total": None,
        "price_target_average": None,
        "price_target_high": None,
        "price_target_low": None,
        "eps_forecast_next_fq": None,
        "eps_forecast_next_fy": None,
        "revenue_forecast_next_fq": None,
        "revenue_forecast_next_fy": None,
        "forward_pe": None,
        "forward_ps": None,
        "forward_ev_ebitda": None,
        "ebitda_estimate_fy": None,
        "free_cash_flow_estimate_fy": None,
        "capex_estimate_fy": None,
        "total_debt_estimate_fy": None,
        "quote_close": None,
        "quote_currency": None,
    }


@pytest.mark.parametrize("analysts", [7, 2**63 - 1])
def test_populated_retained_row_keeps_all_eighteen_native_field_mappings(analysts):
    data = payload(["TSE:1000"])
    data["data"]["TSE:1000"].update(
        AnalystRating="Buy",
        recommendation_total=analysts,
        price_target_average=170,
        price_target_high=220,
        price_target_low=130,
        earnings_per_share_forecast_next_fq=-1.25,
        revenue_forecast_next_fq=200_000,
        revenue_forecast_next_fy=900_000,
        price_earnings_fwd=13.5,
        price_sales_fwd=2.5,
        enterprise_value_ebitda_fwd=7.5,
        ebitda_estimate_fy=300_000,
        free_cash_flow_estimate_fy=-40_000,
        capital_expenditures_estimate_fy=-50_000,
        total_debt_estimate_fy=600_000,
    )
    assert normalize_batch(data, ["TSE:1000"], NOW) == [
        {
            "ticker": "1000",
            "provider_symbol": "TSE:1000",
            "fetched_at_utc": NOW.isoformat(),
            "fetch_status": "ok",
            "analyst_rating": "Buy",
            "recommendation_total": analysts,
            "price_target_average": 170,
            "price_target_high": 220,
            "price_target_low": 130,
            "eps_forecast_next_fq": -1.25,
            "eps_forecast_next_fy": -5,
            "revenue_forecast_next_fq": 200_000,
            "revenue_forecast_next_fy": 900_000,
            "forward_pe": 13.5,
            "forward_ps": 2.5,
            "forward_ev_ebitda": 7.5,
            "ebitda_estimate_fy": 300_000,
            "free_cash_flow_estimate_fy": -40_000,
            "capex_estimate_fy": -50_000,
            "total_debt_estimate_fy": 600_000,
            "quote_close": 100,
            "quote_currency": "JPY",
        }
    ]
