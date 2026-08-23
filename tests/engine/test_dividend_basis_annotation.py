"""The dividend half of carry has to be readable on the surface Research Gate inspects.

`dividend_yield` is one number and a special dividend enters it whole, so a one-off
distribution and a repeatable payout look identical there. Because carry is the primary
ranking key, the one-off lands at the top of the list rather than being lost in it — as-of
2026-08-14 the machine's rank 1 carried a 15.72% dividend yield whose repeatable part was
1.99%. The forecast and the last actual therefore have to appear together, and neither may
touch the rank.
"""

from __future__ import annotations

from datetime import date

from baibai_engine.screening.rule_config import DEFAULT_RULES_PATH, load_screening_rules
from baibai_engine.screening.selection import build_selection_payload, candidate_record_from_mapping

_ASOF = date(2026, 8, 14)

_ANNOTATION_KEYS = frozenset(
    {
        "dividend_yield",
        "dps_forecast_annual",
        "dps_actual_annual",
        "dividend_basis",
        "dividend_split_factor",
    }
)


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
        top=5,
        profile="balanced",
        candidates_ref="test.yaml",
        macro_context_ref=None,
        longlist_top=5,
    )


def test_the_forecast_and_the_last_actual_appear_together() -> None:
    payload = _payload(
        [
            _candidate(
                "1111",
                er_annual=0.18,
                dividend_yield=0.1572,
                dps_forecast_annual=475.0,
                dps_actual_annual=45.0,
                dividend_basis="forecast_annual",
                dividend_split_factor=None,
            )
        ]
    )

    row = payload["longlist"][0]

    assert row["dividend_basis"] == {
        "annual_yield": 0.1572,
        "dps_forecast_annual": 475.0,
        "dps_actual_annual": 45.0,
        "basis": "forecast_annual",
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

    row = payload["longlist"][0]

    assert row["dividend_basis"]["dps_actual_annual"] is None
    assert row["dividend_basis"]["split_factor"] == 5.0


def test_the_annotation_does_not_move_the_rank() -> None:
    """The one-off is surfaced for the human; the machine keeps ranking on E[r]."""

    spiked = _candidate(
        "1111",
        er_annual=0.09,
        dividend_yield=0.0518,
        dps_forecast_annual=475.0,
        dps_actual_annual=45.0,
        dividend_basis="forecast_annual",
    )
    steady = _candidate(
        "2222",
        er_annual=0.12,
        dividend_yield=0.0518,
        dps_forecast_annual=100.0,
        dps_actual_annual=90.0,
        dividend_basis="forecast_annual",
    )

    ranked = [row["ticker"] for row in _payload([spiked, steady])["longlist"]]

    assert ranked == ["2222", "1111"]
