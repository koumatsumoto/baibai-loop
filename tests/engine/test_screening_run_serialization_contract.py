from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.schema import (
    StockAnalysis,
)
from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.screening_run_serializer import (
    stock_analysis_payload,
)


def test_metric_serialization_preserves_all_typed_values_and_rounds_only_floats():
    analysis = StockAnalysis(
        ticker="7203",
        name="test",
        sector_33="test",
        ttm_quality={},
        per_forward=None,
        per_trailing=None,
        pbr=None,
        ev_ebitda=None,
        p_s=None,
        pcfr=None,
        metrics={"ratio": 1.234567, "count": 3, "known": True, "missing": None, "basis": "raw"},
    )
    assert stock_analysis_payload(analysis)["metrics"] == {
        "ratio": 1.2346,
        "count": 3,
        "known": True,
        "missing": None,
        "basis": "raw",
    }
