"""Researchの固定runにだけ適用する保存済み較正文脈。"""

import json
from datetime import datetime

import pytest
import yaml
from tests.helpers.er_calibration_context import context_artifact
from tests.helpers.research_selection import draft_for, selection
from tests.helpers.screening_run import stock_analysis

from baibai_engine.investment_judgment.calibration_context import research_calibration_context
from baibai_engine.investment_judgment.selection import selection_sha256
from baibai_engine.investment_judgment.workspace import prepare_selected_workspace
from baibai_engine.investment_judgment.workspace_cli import main

NOW = datetime.fromisoformat("2026-10-19T22:00:00+09:00")


def _selection(tmp_path):
    return selection(
        now=NOW,
        tickers=("1234",),
        runs=tmp_path / "runs",
        rows=[stock_analysis("1234", name="合成会社", metrics={"er_annual": 0.09})],
    )


def test_fixed_method_context_reports_observed_bands_without_mutating_selection(tmp_path):
    selected = _selection(tmp_path)
    source = selected.inputs.analysis
    path = tmp_path / "context.yaml"
    path.write_text(
        yaml.safe_dump(context_artifact(rules_hash=source.screening_rules_hash)),
        encoding="utf-8",
    )
    before = selection_sha256(selected), path.read_bytes()

    result = research_calibration_context(selected, context_path=path, as_of=NOW.date())

    assert result["status"] == "available"
    assert result["run_revision_id"] == source.run_revision_id
    assert result["context"]["evidence_complete"] is True
    assert result["targets"][0]["er_annual"] == 0.09
    assert [band["band_id"] for band in result["targets"][0]["horizons"][0]["bands"]] == [
        "q5",
        "er_gte_8_5pct",
    ]
    assert (selection_sha256(selected), path.read_bytes()) == before


def test_missing_or_inapplicable_context_is_reasoned_and_nonblocking(tmp_path):
    selected = _selection(tmp_path)
    original = selection_sha256(selected)
    missing = research_calibration_context(
        selected, context_path=tmp_path / "missing.yaml", as_of=NOW.date()
    )
    assert (missing["status"], missing["unavailable_reason"]) == (
        "unavailable",
        "missing_artifact",
    )

    path = tmp_path / "context.yaml"
    path.write_text(yaml.safe_dump(context_artifact()), encoding="utf-8")
    mismatch = research_calibration_context(selected, context_path=path, as_of=NOW.date())
    assert mismatch["unavailable_reason"] == "rules_identity_mismatch"

    assert selection_sha256(selected) == original


@pytest.mark.parametrize("value", [True, None, "not-a-ratio"])
def test_invalid_or_missing_er_is_not_used_as_a_band(tmp_path, value):
    selected = _selection(tmp_path)
    source = selected.inputs.analysis
    row = dict(source.analyses["1234"])
    metrics = dict(row["metrics"])
    if value is None:
        metrics.pop("er_annual", None)
    else:
        metrics["er_annual"] = value
    row["metrics"] = metrics
    analysis = source.model_copy(update={"analyses": {"1234": row}})
    selected = selected.model_copy(
        update={"inputs": selected.inputs.model_copy(update={"analysis": analysis})}
    )
    path = tmp_path / "context.yaml"
    path.write_text(
        yaml.safe_dump(context_artifact(rules_hash=source.screening_rules_hash)),
        encoding="utf-8",
    )
    result = research_calibration_context(selected, context_path=path, as_of=NOW.date())
    assert result["status"] == "available"
    assert result["targets"] == [{"ticker": "1234", "unavailable_reason": "er_unavailable"}]


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_nonfinite_fixed_selection_is_rejected_before_band_lookup(tmp_path, value):
    selected = _selection(tmp_path)
    source = selected.inputs.analysis
    row = dict(source.analyses["1234"])
    row["metrics"] = {**row["metrics"], "er_annual": value}
    analysis = source.model_copy(update={"analyses": {"1234": row}})
    selected = selected.model_copy(
        update={"inputs": selected.inputs.model_copy(update={"analysis": analysis})}
    )
    with pytest.raises(ValueError, match="Out of range float values"):
        research_calibration_context(
            selected, context_path=tmp_path / "unused.yaml", as_of=NOW.date()
        )


def test_missing_fixed_identity_does_not_use_present_day_method(tmp_path):
    selected = selection(now=NOW, tickers=("1234",))
    assert (
        research_calibration_context(
            selected, context_path=tmp_path / "unused.yaml", as_of=NOW.date()
        )["unavailable_reason"]
        == "missing_run_identity"
    )

    with_run = _selection(tmp_path)
    source = with_run.inputs.analysis.model_copy(update={"er_model_version": None})
    without_method = with_run.model_copy(
        update={"inputs": with_run.inputs.model_copy(update={"analysis": source})}
    )
    assert (
        research_calibration_context(
            without_method, context_path=tmp_path / "unused.yaml", as_of=NOW.date()
        )["unavailable_reason"]
        == "missing_method_identity"
    )


def test_cli_uses_saved_operation_and_returns_only_read_only_diagnostics(tmp_path, capsys):
    selected = _selection(tmp_path)
    prepared = prepare_selected_workspace(
        draft=draft_for(selected),
        db_path=tmp_path / "app.sqlite",
        workspace=tmp_path / "workspace",
        sqlite_path=tmp_path / "absent-market",
        runs_path=tmp_path / "runs",
        indicators_path=tmp_path / "absent-indicators",
        now=NOW,
    )
    path = tmp_path / "context.yaml"
    path.write_text(
        yaml.safe_dump(context_artifact(rules_hash=selected.inputs.analysis.screening_rules_hash)),
        encoding="utf-8",
    )
    selection_bytes = (tmp_path / "workspace/domain-inputs.yaml").read_bytes()
    app_bytes = (tmp_path / "app.sqlite").read_bytes()
    args = [
        "calibration-context",
        "--operation-id",
        prepared.operation_id,
        "--db",
        str(tmp_path / "app.sqlite"),
        "--context-path",
        str(path),
        "--asof",
        "2026-10-19",
    ]

    assert main(args, now=NOW) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["operation_id"] == prepared.operation_id
    assert output["selection_sha256"] == prepared.selection_sha256
    assert output["status"] == "available"
    assert (tmp_path / "workspace/domain-inputs.yaml").read_bytes() == selection_bytes
    assert (tmp_path / "app.sqlite").read_bytes() == app_bytes
    missing_args = [*args[:5], "--context-path", str(tmp_path / "missing.yaml")]
    assert main(missing_args, now=NOW) == 0
    assert json.loads(capsys.readouterr().out)["unavailable_reason"] == "missing_artifact"
    assert main([*missing_args[:2], "unknown-operation", *missing_args[3:]], now=NOW) == 3
    assert json.loads(capsys.readouterr().out)["status"] == "invalid_source"
    assert (tmp_path / "app.sqlite").read_bytes() == app_bytes


def test_calibration_cli_rejects_invalid_asof_before_reading_operation(tmp_path):
    with pytest.raises(SystemExit) as error:
        main(
            [
                "calibration-context",
                "--operation-id",
                "unread-operation",
                "--db",
                str(tmp_path / "missing.sqlite"),
                "--asof",
                "2026-13-19",
            ]
        )
    assert error.value.code == 2
    assert not (tmp_path / "missing.sqlite").exists()
