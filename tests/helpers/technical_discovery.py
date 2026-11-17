"""保存TAの時点・欠測を検証する共通合成入力。"""

from datetime import UTC, date, datetime

from baibai_engine.machine_analysis.generation.contracts import (
    GeneratedModel,
    GenerationMetadata,
    GenerationSubject,
)
from tests.helpers.model_generation import generation

_DAY = "2026-11-13"


def _metadata():
    return GenerationMetadata(
        generation_id="technical-fixed",
        as_of=date(2026, 11, 14),
        evaluation_time=datetime(2026, 11, 14, 8, 37, tzinfo=UTC),
        inputs=generation().inputs,
        schema_version=2,
        subject_plan={
            "1234": GenerationSubject(
                scope="stock",
                model_ids=("technical_analysis", "market_context", "company_financials"),
            ),
            "5678": GenerationSubject(
                scope="stock", model_ids=("technical_analysis", "market_context")
            ),
            "market": GenerationSubject(scope="market", model_ids=("market_context",)),
        },
    )


def _models():
    path = {
        "expected_price_date": _DAY,
        "latest_observed_price_date": _DAY,
        "current_row_state": "priced",
        "current_price": 110.0,
        "freshness": "current",
        "price_basis": "price_adjusted_close",
        "observations": {
            "R1": {
                "value": 0.01,
                "unavailable_reason": None,
                "window_start_date": "2026-11-12",
                "window_end_date": _DAY,
            },
            "R20": {
                "value": 0.1,
                "unavailable_reason": None,
                "window_start_date": "2026-10-15",
                "window_end_date": _DAY,
            },
            "G60": {
                "value": -0.25,
                "window_return": 0.15,
                "window_return_unavailable_reason": None,
                "window_start_date": "2026-08-14",
                "window_end_date": _DAY,
            },
        },
    }
    ta = {
        "price_basis": "price_adjusted_close",
        "price_path": path,
        "price_momentum": {
            "status": "available",
            "value": 0.35,
            "reference_date": _DAY,
            "start_date": "2025-10-29",
            "end_date": "2026-10-14",
        },
        "moving_averages": {
            f"sma_{n}bar": {
                "status": "available",
                "gap_ratio": gap,
                "window_bars": n,
                "window_start_date": "2026-01-01",
                "window_end_date": _DAY,
            }
            for n, gap in ((20, 0.02), (50, 0.05), (200, 0.2))
        },
    }
    market = {
        "master": {"is_common_stock": True},
        "price_path_relative": {
            "benchmark_ticker": "1321",
            "price_basis": "price_adjusted_close",
            "comparisons": {
                "R20": {
                    "status": "available",
                    "relative_return": 0.05,
                    "own_return": 0.1,
                    "comparison_start_date": "2026-10-15",
                    "comparison_end_date": _DAY,
                }
            },
        },
        "price": {
            "turnover_activity_20d": {
                "status": "available",
                "current_to_prior_mean_ratio": 2.0,
                "current_date": _DAY,
                "prior_start_date": "2026-10-15",
                "prior_end_date": "2026-11-12",
                "prior_valid_days": 20,
                "basis": "traded_value_yen_not_share_volume",
                "window_basis": "prior_20_common_trading_sessions_excluding_current",
            }
        },
    }
    return tuple(
        GeneratedModel(
            model_id=name, subject="1234", calculation_revision="stored-v1", payload=payload
        )
        for name, payload in (("technical_analysis", ta), ("market_context", market))
    )
