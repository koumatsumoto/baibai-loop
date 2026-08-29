"""Research Gate sees the source amounts behind the dividend carry choice.

The resolver falls back to actual DPS when a forecast exceeds twice the positive actual,
but the raw forecast and actual still appear together. The surface lets a human distinguish
a one-off distribution, a genuine payout-policy change, and a share-basis issue without
adding another warning or special-dividend state.
"""

from __future__ import annotations

from datetime import date

from baibai_engine.screening.rule_config import DEFAULT_RULES_PATH, load_screening_rules
from baibai_engine.screening.selection import build_selection_payload, candidate_record_from_mapping

_ASOF = date(2026, 8, 14)


def _candidate(ticker: str, **metrics: object) -> dict[str, object]:
    return {
        "ticker": ticker,
        "name": f"name-{ticker}",
        "sector_33": "機械",
        "market_cap_oku": 300,
        "avg_turnover_oku": 2.0,
        "listing_span_days": 1200,
        "jpx_flags": [],
        "evidence_hits": [{"name": "cashflow-yield-discount"}],
        "metrics": {"ocf_yield": 0.11, **metrics},
    }


def _payload(candidates: list[dict[str, object]]) -> dict[str, object]:
    return build_selection_payload(
        asof_date=_ASOF,
        candidates=tuple(candidate_record_from_mapping(item) for item in candidates),
        macro_context=None,
        rules=load_screening_rules(DEFAULT_RULES_PATH),
        candidates_ref="test.yaml",
        macro_context_ref=None,
        review_cap=5,
    )


def test_the_forecast_and_the_last_actual_appear_together() -> None:
    payload = _payload(
        [
            _candidate(
                "1111",
                er_annual=0.04,
                dividend_yield=0.0143,
                dps_forecast_annual=475.0,
                dps_actual_annual=45.0,
                dividend_basis="actual_reported",
                dividend_split_factor=None,
            )
        ]
    )

    row = payload["ranked_set"][0]

    assert row["dividend_basis"] == {
        "annual_yield": 0.0143,
        "dps_forecast_annual": 475.0,
        "dps_actual_annual": 45.0,
        "basis": "actual_reported",
        "split_factor": None,
    }


def test_an_unresolvable_share_basis_stays_null_rather_than_reading_as_no_dividend() -> None:
    """A year whose split basis could not be resolved is unknown, not zero."""

    payload = _payload(
        [
            _candidate(
                "1111",
                er_annual=0.09,
                dividend_yield=0.05,
                dps_forecast_annual=100.0,
                dps_actual_annual=None,
                dividend_basis="forecast_annual",
                dividend_split_factor=5.0,
            )
        ]
    )

    row = payload["ranked_set"][0]

    assert row["dividend_basis"]["dps_actual_annual"] is None
    assert row["dividend_basis"]["split_factor"] == 5.0
