"""研究用TA入口の意味・欠測と、固定世代の読取境界。利益効果のtestではない。"""

import gzip
import json
import sqlite3
from copy import deepcopy
from datetime import UTC, date, datetime

import pytest
from tests.helpers.model_generation import generation, publish_generation
from tools.experiments.technical_discovery import (
    evaluate_technical_discovery,
    inspect_technical_discovery,
    main,
)

from baibai_engine.machine_analysis.generation.contracts import (
    GeneratedModel,
    GenerationMetadata,
    GenerationSubject,
    ModelGeneration,
)

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


def _evaluate(models=None):
    return evaluate_technical_discovery(
        "1234", models=_models() if models is None else models, metadata=_metadata()
    )


def test_independent_routes_use_window_return_not_peak_drawdown_and_keep_source_clock():
    models = _models()
    before = deepcopy(models)
    row = _evaluate(models)
    assert row["matched_routes"] == [
        "market_outperformance",
        "trend_continuation",
        "turnover_expansion",
    ]
    assert row["observations"]["window_return_60d"]["value"] == 0.15
    assert row["observations"]["window_return_60d"]["source_field"].endswith("G60.window_return")
    assert row["routes"]["rebound_start"]["state"] == "not_matched"
    assert row["as_of"] == "2026-11-14"
    assert row["expected_price_date"] == _DAY
    assert row["market_relative_benchmark"] == "1321"
    assert row["observations"]["momentum_12_1"]["window"]["end_date"] == "2026-10-14"
    assert "rank" not in row
    assert "score" not in row
    assert models == before


def test_rebound_relative_resilience_and_pullback_do_not_require_all_routes():
    models = _models()
    models[0].payload["price_path"]["observations"]["R20"]["value"] = -0.1
    models[0].payload["moving_averages"]["sma_20bar"]["gap_ratio"] = -0.01
    models[1].payload["price_path_relative"]["comparisons"]["R20"]["own_return"] = -0.1
    row = _evaluate(models)
    assert "trend_continuation" not in row["matched_routes"]
    assert {"market_outperformance", "trend_pullback", "rebound_start"} <= set(
        row["matched_routes"]
    )
    assert row["observations"]["R20"]["value"] < 0


@pytest.mark.parametrize("invalid", ["foreign_subject", "duplicate_model"])
def test_pure_api_rejects_mixed_subjects_and_duplicate_inputs(invalid):
    models = _models()
    if invalid == "foreign_subject":
        models = (models[0].model_copy(update={"subject": "5678"}), models[1])
    else:
        models = (*models, models[0])
    with pytest.raises(ValueError, match="technical discovery requires"):
        _evaluate(models)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("current_row_state", "no_trade"),
        ("freshness", "stale"),
        ("latest_observed_price_date", "2026-11-12"),
        ("expected_price_date", "2026-11-16"),
        ("current_price", 0),
        ("price_basis", "raw_close"),
    ],
)
def test_retained_strong_indicators_never_match_without_current_price_context(field, value):
    models = _models()
    models[0].payload["price_path"][field] = value
    row = _evaluate(models)
    assert row["matched_routes"] == []
    assert {route["state"] for route in row["routes"].values()} == {"unavailable"}
    assert row["price_context"]["ready"] is False


@pytest.mark.parametrize(
    ("mutation", "expected_state"),
    [
        ("absent", "not_recorded"),
        ("null", "recorded_null"),
        ("model_missing", "model_not_generated"),
    ],
)
def test_unrecorded_null_and_missing_model_remain_distinct(mutation, expected_state):
    models = _models()
    if mutation == "absent":
        del models[0].payload["price_momentum"]
    elif mutation == "null":
        models[0].payload["price_momentum"] = None
    else:
        models = models[1:]
    row = _evaluate(models)
    assert row["observations"]["momentum_12_1"]["source_state"] == expected_state
    assert row["routes"]["trend_continuation"]["state"] == "unavailable"
    assert ("technical_analysis" in row["missing_models"]) == (mutation == "model_missing")


def test_failure_does_not_erase_missing_condition_and_zero_is_not_missing():
    models = _models()
    models[0].payload["price_path"]["observations"]["R20"]["value"] = 0
    models[0].payload["price_momentum"]["value"] = None
    row = _evaluate(models)
    route = row["routes"]["trend_continuation"]
    assert route["state"] == "not_matched"
    assert route["failed_conditions"] == ["R20_positive"]
    assert route["missing_conditions"] == ["momentum_12_1_positive"]
    assert row["observations"]["R20"]["value"] == 0
    assert row["observations"]["R20"]["unavailable_reason"] is None


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("comparison_start_date", "2026-10-14", "comparison_window_mismatch"),
        ("comparison_end_date", "2026-11-12", "price_date_mismatch_or_unknown"),
    ],
)
def test_market_relative_requires_the_same_r20_endpoints(field, value, reason):
    models = _models()
    models[1].payload["price_path_relative"]["comparisons"]["R20"][field] = value
    row = _evaluate(models)
    assert row["routes"]["market_outperformance"]["state"] == "unavailable"
    assert row["observations"]["market_relative_20d"]["unavailable_reason"] == reason
    assert row["routes"]["trend_continuation"]["state"] == "matched"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, "0.2"])
def test_invalid_values_are_unavailable_not_positive_votes(value):
    models = _models()
    models[0].payload["price_momentum"]["value"] = value
    row = _evaluate(models)
    assert row["observations"]["momentum_12_1"]["unavailable_reason"] == "nonfinite_or_non_numeric"
    assert row["routes"]["trend_continuation"]["state"] == "unavailable"
    json.dumps(row, allow_nan=False)


def test_activity_never_substitutes_share_volume_current_inclusive_mean_or_missing_source():
    models = _models()
    activity = models[1].payload["price"]["turnover_activity_20d"]
    activity["window_basis"] = "observed_20bar_including_current"
    row = _evaluate(models)
    assert row["routes"]["turnover_expansion"]["state"] == "unavailable"
    assert (
        row["observations"]["turnover_activity_20d"]["unavailable_reason"]
        == "turnover_basis_mismatch_or_unknown"
    )
    del models[1].payload["price"]["turnover_activity_20d"]
    models[1].payload["price"]["avg_turnover_20d_oku"] = 99
    assert _evaluate(models)["routes"]["turnover_expansion"]["state"] == "unavailable"


def _seed(path):
    metadata = _metadata()
    extra = (
        GeneratedModel(
            model_id="company_financials",
            subject="1234",
            calculation_revision="v1",
            payload={"reject": True},
        ),
        GeneratedModel(
            model_id="market_context", subject="market", calculation_revision="v1", payload={}
        ),
    )
    fixed = ModelGeneration(**metadata.model_dump(), models=(*_models(), *extra))
    publish_generation(fixed, path=path)
    publish_generation(fixed.model_copy(update={"generation_id": "other"}), path=path)
    with sqlite3.connect(path) as c:
        c.execute(
            "UPDATE generated_model SET payload_gzip=? WHERE generation_id!='technical-fixed' OR model_id='company_financials' OR subject='market'",
            (b"must-not-read",),
        )


def test_reader_preserves_the_whole_plan_without_reading_financials_or_other_generations(tmp_path):
    path = tmp_path / "runs.sqlite"
    _seed(path)
    before = path.read_bytes()
    result = inspect_technical_discovery(generation_id="technical-fixed", runs_path=path)
    first, missing = result["rows"]
    assert first["ticker"] == "1234"
    assert first["matched_routes"]
    assert missing["ticker"] == "5678"
    assert missing["matched_routes"] == []
    assert missing["missing_models"] == ["technical_analysis", "market_context"]
    assert result["summary"]["stocks"] == 2
    assert result["summary"]["matched_any_route"] == 1
    assert path.read_bytes() == before


def test_empty_saved_plan_is_explicit_not_a_missing_generation(tmp_path):
    path = tmp_path / "runs.sqlite"
    metadata = _metadata().model_copy(update={"subject_plan": {}})
    publish_generation(ModelGeneration(**metadata.model_dump(), models=()), path=path)
    result = inspect_technical_discovery(generation_id="technical-fixed", runs_path=path)
    assert result["rows"] == []
    assert result["summary"] == {"stocks": 0, "matched_any_route": 0}


@pytest.mark.parametrize("broken", [b"not-gzip", gzip.compress(b"{}")[:-1]])
def test_failed_read_never_creates_a_complete_output_or_logs_payload(tmp_path, capsys, broken):
    path = tmp_path / "runs.sqlite"
    _seed(path)
    with sqlite3.connect(path) as c:
        c.execute(
            "INSERT INTO generated_model VALUES (?,?,?,?,?)",
            ("technical-fixed", "technical_analysis", "5678", "v1", broken),
        )
    out = tmp_path / "result.json"
    assert (
        main(["--generation-id", "technical-fixed", "--runs-path", str(path), "--out", str(out)])
        == 2
    )
    assert not out.exists()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "technical discovery failed" in captured.err


def test_cli_missing_store_and_unknown_generation_are_not_empty_success(tmp_path, capsys):
    path = tmp_path / "missing.sqlite"
    args = ["--generation-id", "technical-fixed", "--runs-path", str(path)]
    assert main(args) == 2
    assert not path.exists()
    _seed(path)
    args[1] = "missing-generation"
    assert main(args) == 2
    assert capsys.readouterr().out == ""


def test_cli_does_not_overwrite_and_v1_without_plan_is_not_reconstructed(tmp_path):
    path = tmp_path / "runs.sqlite"
    _seed(path)
    out = tmp_path / "result.json"
    args = ["--generation-id", "technical-fixed", "--runs-path", str(path), "--out", str(out)]
    assert main(args) == 0
    before = out.read_bytes()
    assert main(args) == 2
    assert out.read_bytes() == before
    with sqlite3.connect(path) as c:
        header = json.loads(
            c.execute(
                "SELECT header FROM model_generation WHERE generation_id='technical-fixed'"
            ).fetchone()[0]
        )
        header["schema_version"] = 1
        header["subject_plan"] = None
        c.execute(
            "UPDATE model_generation SET header=? WHERE generation_id='technical-fixed'",
            (json.dumps(header),),
        )
    with pytest.raises(ValueError, match="saved stock plan"):
        inspect_technical_discovery(generation_id="technical-fixed", runs_path=path)
