"""保存されたTAの状態比較を検証する。発見品質・市場PIT・利益のtestではない。"""

import json
import sqlite3
from copy import deepcopy
from datetime import date, timedelta

import pytest
from tests.helpers.model_generation import publish_generation
from tests.tools.test_technical_discovery import _metadata, _models
from tools.experiments import inspect_technical_transitions as transition
from tools.experiments.technical_discovery import evaluate_technical_discovery

from baibai_engine.machine_analysis.generation.contracts import GenerationSubject, ModelGeneration


def _shift(value, days):
    if isinstance(value, dict):
        return {key: _shift(item, days) for key, item in value.items()}
    if isinstance(value, list):
        return [_shift(item, days) for item in value]
    if isinstance(value, str) and len(value) == 10 and value[4] == value[7] == "-":
        return (date.fromisoformat(value) + timedelta(days=days)).isoformat()
    return value


def _generation(ident, *, days=0, models=None, tickers=("1234",)):
    metadata = _metadata().model_copy(
        update={
            "generation_id": ident,
            "as_of": _metadata().as_of + timedelta(days=days),
            "evaluation_time": _metadata().evaluation_time + timedelta(days=days),
            "subject_plan": {
                ticker: GenerationSubject(
                    scope="stock", model_ids=("technical_analysis", "market_context")
                )
                for ticker in tickers
            },
        }
    )
    originals = _models() if models is None else models
    shifted = tuple(
        model.model_copy(update={"subject": ticker, "payload": _shift(model.payload, days)})
        for ticker in tickers
        for model in originals
    )
    return ModelGeneration(**dict(metadata), models=shifted)


def _snapshot(generation):
    metadata = generation.model_copy(update={"models": ()})
    rows = [
        evaluate_technical_discovery(
            ticker,
            models=tuple(model for model in generation.models if model.subject == ticker),
            metadata=metadata,
        )
        for ticker in sorted(generation.subject_plan)
    ]
    return {
        "generation_id": generation.generation_id,
        "as_of": generation.as_of.isoformat(),
        "evaluation_time": generation.evaluation_time.isoformat(),
        "policy_id": "technical-discovery-research-v1",
        "rows": rows,
    }


def _inspect(monkeypatch, previous, current):
    snapshots = {item.generation_id: _snapshot(item) for item in (previous, current)}
    monkeypatch.setattr(
        transition,
        "inspect_technical_discovery",
        lambda *, generation_id, runs_path: snapshots[generation_id],
    )
    return transition.inspect_technical_transitions(
        previous_generation_id=previous.generation_id,
        current_generation_id=current.generation_id,
        runs_path=None,
    )


def _relative(models, value):
    models[1].payload["price_path_relative"]["comparisons"]["R20"]["relative_return"] = value


def _args(path, out, *, previous="before", current="after"):
    return [
        "--previous-generation-id",
        previous,
        "--current-generation-id",
        current,
        "--runs-path",
        str(path),
        "--out",
        str(out),
    ]


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        (-0.1, 0.1, "new_match"),
        (0.1, 0.1, "continuing_match"),
        (0.1, -0.1, "ended_match"),
        (-0.1, -0.1, "continuing_nonmatch"),
    ],
)
def test_route_state_transitions_keep_static_matches_and_source_evidence(
    monkeypatch, left, right, expected
):
    before_models, after_models = _models(), _models()
    _relative(before_models, left)
    _relative(after_models, right)
    before, after = (
        _generation("before", days=-1, models=before_models),
        _generation("after", models=after_models),
    )
    original = deepcopy((before, after))
    result = _inspect(monkeypatch, before, after)
    row = result["rows"][0]
    route = result["summary"]["routes"]["market_outperformance"]
    assert row["route_changes"]["market_outperformance"]["transition"] == expected
    assert route["known_state_pairs"] == 1
    assert sum(route["transitions"].values()) == 1
    assert row["comparison"] == {"comparable": True, "reasons": []}
    assert row["previous"]["observations"]["market_relative_20d"]["value"] == left
    assert row["current"]["observations"]["market_relative_20d"]["value"] == right
    assert (before, after) == original
    assert result["summary"]["new_match_any_route"] == (expected == "new_match")
    assert result["summary"]["new_match_already_in_previous_union"] == (expected == "new_match")
    assert result["adoption_status"] == "unadopted_research_only"
    assert result["current"]["as_of"] == "2026-11-14"
    assert row["current"]["expected_price_date"] == "2026-11-13"
    assert "prospective_enrollment_or_tradable_event_time" in result["not_measured"]


@pytest.mark.parametrize(
    ("left_missing", "right_missing", "expected"),
    [
        (True, False, "input_became_observable"),
        (False, True, "input_became_unobservable"),
        (True, True, "input_unresolved"),
    ],
)
def test_unavailable_is_not_a_negative_state_or_zero_measured_events(
    monkeypatch, left_missing, right_missing, expected
):
    before_models, after_models = _models(), _models()
    for missing, models in ((left_missing, before_models), (right_missing, after_models)):
        if missing:
            models[1].payload["price"].pop("turnover_activity_20d")
    result = _inspect(
        monkeypatch,
        _generation("before", days=-1, models=before_models),
        _generation("after", models=after_models),
    )
    row = result["rows"][0]
    assert row["route_changes"]["turnover_expansion"]["transition"] == expected
    assert "turnover_expansion" not in row["newly_matched_routes"]
    route = result["summary"]["routes"]["turnover_expansion"]
    assert route["known_state_pairs"] == 0
    assert route["transitions"]["new_match"] == 0
    assert route["transitions"][expected] == 1


def test_known_failure_plus_missing_is_not_pure_new_match(monkeypatch):
    models = _models()
    models[0].payload["price_path"]["observations"]["R20"]["value"] = -0.1
    models[0].payload["moving_averages"]["sma_200bar"]["gap_ratio"] = None
    result = _inspect(
        monkeypatch, _generation("before", days=-1, models=models), _generation("after")
    )
    row = result["rows"][0]
    source = row["previous"]["routes"]["trend_continuation"]
    assert source["state"] == "not_matched"
    assert source["failed_conditions"]
    assert source["missing_conditions"]
    assert row["route_changes"]["trend_continuation"]["transition"] == "input_became_observable"
    assert "trend_continuation" not in row["newly_matched_routes"]


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("same_day", "same_price_date"),
        ("reversed", "price_date_moved_backwards"),
        ("gap", "adjacent_price_sessions_not_established"),
        ("end", "adjacent_price_sessions_not_established"),
        ("recipe", "model_revision_mismatch"),
        ("no_trade", "previous_price_not_ready"),
        ("missing_model", "model_not_generated"),
    ],
)
def test_noncomparable_inputs_retain_states_but_never_create_new_match(monkeypatch, change, reason):
    before_models, after_models = _models(), _models()
    _relative(before_models, -0.1)
    before = _generation("before", days=-1, models=before_models)
    after = _generation("after", models=after_models)
    if change == "same_day":
        before = _generation("before", models=before_models)
    elif change == "reversed":
        after = _generation("after", days=-2)
    elif change in ("gap", "end"):
        field = "window_start_date" if change == "gap" else "window_end_date"
        after.result("technical_analysis", "1234").payload["price_path"]["observations"]["R1"][
            field
        ] = "2026-11-11"
    elif change == "recipe":
        after = after.model_copy(
            update={
                "models": tuple(
                    model.model_copy(update={"calculation_revision": "new"})
                    for model in after.models
                )
            }
        )
    elif change == "no_trade":
        before.result("technical_analysis", "1234").payload["price_path"]["current_row_state"] = (
            "no_trade"
        )
    else:
        before = before.model_copy(update={"models": ()})
    result = _inspect(monkeypatch, before, after)
    assert reason in result["rows"][0]["comparison"]["reasons"]
    assert result["summary"]["comparable_price_pairs"] == 0
    assert result["summary"]["new_match_any_route"] == 0
    assert result["summary"]["current_matched_any_route"] == 1
    assert all(
        item["transition"] == "not_comparable"
        for item in result["rows"][0]["route_changes"].values()
    )


def test_clock_and_policy_mismatch_are_explicit_and_not_events(monkeypatch):
    before, after = _generation("before", days=-1), _generation("after")
    snapshots = {"before": _snapshot(before), "after": _snapshot(after)}
    snapshots["after"]["policy_id"] = "other-policy"
    snapshots["after"]["evaluation_time"] = snapshots["before"]["evaluation_time"]
    snapshots["after"]["as_of"] = "2026-11-12"
    for row in snapshots["after"]["rows"]:
        for field in ("policy_id", "evaluation_time", "as_of"):
            row[field] = snapshots["after"][field]
    monkeypatch.setattr(
        transition,
        "inspect_technical_discovery",
        lambda *, generation_id, runs_path: snapshots[generation_id],
    )
    result = transition.inspect_technical_transitions(
        previous_generation_id="before", current_generation_id="after", runs_path=None
    )
    assert result["rows"][0]["comparison"]["reasons"] == [
        "discovery_policy_mismatch",
        "generation_evaluation_not_increasing",
        "as_of_moved_backwards",
    ]
    assert result["summary"]["new_match_any_route"] == 0


def test_both_plans_and_missing_models_remain_in_denominators(tmp_path):
    path = tmp_path / "runs"
    before = _generation("before", days=-1, tickers=("1234", "2345", "3456"))
    after = _generation("after", tickers=("1234", "3456", "4567"))
    after = after.model_copy(
        update={"models": tuple(model for model in after.models if model.subject != "3456")}
    )
    for generation in (before, after):
        publish_generation(generation, path=path)
    original = path.read_bytes()
    result = transition.inspect_technical_transitions(
        previous_generation_id="before", current_generation_id="after", runs_path=path
    )
    summary = result["summary"]
    assert summary["previous_planned_stocks"] == summary["current_planned_stocks"] == 3
    assert summary["union_planned_stocks"] == 4
    assert summary["plan_membership"] == {"both": 2, "previous_only": 1, "current_only": 1}
    assert summary["comparable_price_pairs"] == 1
    rows = {row["ticker"]: row for row in result["rows"]}
    assert rows["2345"]["current"] is None
    assert rows["4567"]["previous"] is None
    assert rows["3456"]["current"]["missing_models"] == ["technical_analysis", "market_context"]
    for route in summary["routes"].values():
        assert sum(route["transitions"].values()) == 4
    assert path.read_bytes() == original


@pytest.mark.parametrize("empty", [False, True])
def test_same_generation_and_empty_plans_are_not_new_events(tmp_path, empty):
    path = tmp_path / "runs"
    publish_generation(_generation("fixed", tickers=() if empty else ("1234",)), path=path)
    result = transition.inspect_technical_transitions(
        previous_generation_id="fixed", current_generation_id="fixed", runs_path=path
    )
    assert result["summary"]["new_match_any_route"] == 0
    assert result["summary"]["comparable_price_pairs"] == 0
    assert result["summary"]["union_planned_stocks"] == (0 if empty else 1)
    assert "same_generation" in result["generation_comparison_reasons"]
    if not empty:
        assert "same_generation" in result["rows"][0]["comparison"]["reasons"]


def test_cli_repeat_is_read_only_and_byte_identical(tmp_path):
    path = tmp_path / "runs"
    for generation in (_generation("before", days=-1), _generation("after")):
        publish_generation(generation, path=path)
    original = path.read_bytes()
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    assert transition.main(_args(path, first)) == transition.main(_args(path, second)) == 0
    assert first.read_bytes() == second.read_bytes()
    assert json.loads(first.read_text())["summary"]["union_planned_stocks"] == 1
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "failure",
    ["missing_store", "unknown_previous", "unknown_current", "broken_current", "foreign_row"],
)
def test_failed_read_or_identity_never_exports_partial_result(tmp_path, monkeypatch, failure):
    path, out = tmp_path / "runs", tmp_path / "out.json"
    if failure != "missing_store":
        for generation in (_generation("before", days=-1), _generation("after")):
            publish_generation(generation, path=path)
    previous, current = "before", "after"
    if failure == "unknown_previous":
        previous = "absent"
    elif failure == "unknown_current":
        current = "absent"
    elif failure == "broken_current":
        with sqlite3.connect(path) as connection:
            connection.execute(
                "UPDATE generated_model SET payload_gzip=? WHERE generation_id=?",
                (b"broken", "after"),
            )
    elif failure == "foreign_row":
        real_inspect = transition.inspect_technical_discovery

        def foreign(**kwargs):
            result = real_inspect(**kwargs)
            result["rows"][0]["generation_id"] = "foreign"
            return result

        monkeypatch.setattr(transition, "inspect_technical_discovery", foreign)
    assert transition.main(_args(path, out, previous=previous, current=current)) == 2
    assert not out.exists()
    if failure == "missing_store":
        assert not path.exists()


@pytest.mark.parametrize("symlink", [False, True])
def test_existing_output_is_rejected_before_read(tmp_path, monkeypatch, symlink):
    out = tmp_path / "out.json"
    if symlink:
        out.symlink_to(tmp_path / "missing-target")
    else:
        out.write_text("keep")

    def forbidden(**kwargs):
        pytest.fail("existing output must be rejected before reading")

    monkeypatch.setattr(transition, "inspect_technical_transitions", forbidden)
    assert transition.main(_args(tmp_path / "missing", out)) == 2
    if symlink:
        assert out.is_symlink()
        assert not out.exists()
    else:
        assert out.read_text() == "keep"
