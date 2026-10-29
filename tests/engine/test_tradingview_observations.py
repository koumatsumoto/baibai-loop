"""Retained TradingView normalization and exact saved universe."""

import copy
from contextlib import closing
from pathlib import Path

import pytest
from tests.helpers.master import insert_master_rows
from tests.helpers.tradingview import DAY, NOW, payload, store

from baibai_engine.machine_analysis.observations.external_assessments.tradingview.observations import (
    FetchError,
    normalize_batch,
)
from baibai_engine.machine_analysis.observations.jquants_decode import JQuantsProviderError
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.maintenance.tradingview_context import universe


def test_universe_excludes_secondary_instruments_without_bar_history(tmp_path: Path) -> None:
    path = store(tmp_path / "market.sqlite", 1)
    conn = open_connection(path)
    with conn:
        insert_master_rows(
            conn,
            [
                (DAY.isoformat(), "2000", "fund", "プライム", "その他", "014"),
                (DAY.isoformat(), "2001", "pro", "TOKYO PRO MARKET", "機械", "011"),
                (DAY.isoformat(), "2002", "Nagoya", "名証", "機械", "011"),
            ],
        )
    assert universe(conn, DAY) == ["TSE:1000"]
    with conn:
        conn.execute(
            "UPDATE jquants_master_snapshots SET record_json=json_set(record_json,'$.ProdCat',NULL) WHERE security_code='10000'"
        )
    with pytest.raises(JQuantsProviderError, match="product category"):
        universe(conn, DAY)
    conn.close()


@pytest.mark.parametrize("forecast", [None, 0, -5])
def test_normal_null_zero_and_negative_forecast_are_not_unresolved(forecast) -> None:
    data = payload(["TSE:1000"])
    data["data"]["TSE:1000"]["earnings_per_share_forecast_next_fy"] = forecast
    result = normalize_batch(data, ["TSE:1000"], NOW)
    assert result[0]["fetch_status"] == "ok"
    assert result[0]["recommendation_total"] is None
    assert result[0]["eps_forecast_next_fy"] == forecast


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


def test_native_product_category_does_not_depend_on_sector_spelling(tmp_path):
    path = store(tmp_path / "market.sqlite", 1)
    with closing(open_connection(path)) as conn, conn:
        conn.execute(
            "UPDATE jquants_master_snapshots "
            "SET record_json=json_set(record_json,'$.S33Nm','新業種')"
        )
        assert universe(conn, DAY) == ["TSE:1000"]


@pytest.mark.parametrize(
    ("provider_field", "field", "value", "reason"),
    [
        ("close", "quote_close", float("nan"), "invalid_numeric"),
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
