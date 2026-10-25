from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from baibai_engine.machine_analysis.screening.schema import ScreeningRunDocument, StockAnalysis
from baibai_engine.machine_analysis.screening.screening_run_serializer import (
    screening_run_payload,
    stock_analysis_payload,
)


def _document(run_at):
    return ScreeningRunDocument(
        run_date=date(2026, 10, 15),
        asof_date=date(2026, 10, 15),
        universe_size=0,
        filters={},
        stock_analyses=(),
        run_at=run_at,
        run_id="test",
        screening_rules_hash="rules",
        er_model_version="model",
    )


def test_new_run_export_normalizes_aware_offsets_without_changing_the_instant():
    utc = datetime(2026, 10, 15, 22, tzinfo=UTC)
    jst = utc.astimezone(timezone(timedelta(hours=9)))
    exported = screening_run_payload(_document(utc))
    assert exported == screening_run_payload(_document(jst))
    assert exported["run_at"] == "2026-10-16T07:00:00+09:00"
    assert exported["asof_date"] == "2026-10-15"


def test_document_rejects_missing_timezone_before_serialization():
    with pytest.raises(ValueError, match="timezone-aware"):
        _document(datetime.fromisoformat("2026-10-15T22:00:00"))


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
