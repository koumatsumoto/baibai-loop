"""TA→財務の接続だけを検証し、個々の政策条件をcallerへ複写しない。"""

import json
import sqlite3
from contextlib import contextmanager
from copy import deepcopy

import pytest
from tests.helpers.model_generation import publish_generation
from tests.tools.test_financial_growth_gate import payloads
from tests.tools.test_technical_discovery import _metadata, _models
from tools.experiments import inspect_technical_financial_candidates as integration

from baibai_engine.machine_analysis.generation.contracts import (
    GeneratedModel,
    GenerationSubject,
    ModelGeneration,
)
from baibai_engine.machine_analysis.generation.query import stream_generation_subjects

_IDS = ("technical_analysis", "market_context", "company_financials", "company_growth")
_TICKERS = ("1234", "2345", "3456", "4567", "5678", "6789")


def _no_match(models):
    technical, market = models
    technical.payload["price_momentum"]["value"] = -0.2
    technical.payload["price_path"]["observations"]["R1"]["value"] = -0.1
    technical.payload["price_path"]["observations"]["R20"]["value"] = -0.2
    market.payload["price_path_relative"]["comparisons"]["R20"]["relative_return"] = -0.1
    market.payload["price"]["turnover_activity_20d"]["current_to_prior_mean_ratio"] = 0.5


def _seed(path, *, all_nonmatching=False, tickers=_TICKERS):
    metadata = _metadata().model_copy(
        update={
            "subject_plan": {
                **{ticker: GenerationSubject(scope="stock", model_ids=_IDS) for ticker in tickers},
                "market": GenerationSubject(scope="market", model_ids=("market_context",)),
            }
        }
    )
    models = []
    for ticker in tickers:
        if ticker != "5678":
            technical = tuple(model.model_copy(update={"subject": ticker}) for model in _models())
            if ticker == "4567" or all_nonmatching:
                _no_match(technical)
            models.extend(technical)
        if ticker == "6789":
            continue
        financial = payloads()
        financial["company_growth"]["ticker"] = ticker
        if ticker == "2345":
            balance = financial["company_growth"]["periods"][0]["observations"][-1]
            balance["metrics"]["net_assets"]["value"] = -10.0
        if ticker == "3456":
            financial.pop("company_growth")
        models.extend(
            GeneratedModel(
                model_id=name, subject=ticker, calculation_revision="synthetic-v1", payload=value
            )
            for name, value in financial.items()
        )
    fixed = ModelGeneration(**dict(metadata), models=tuple(models))
    publish_generation(fixed, path=path)
    return fixed


def _args(path, out):
    return ["--generation-id", "technical-fixed", "--runs-path", str(path), "--out", str(out)]


def test_all_denominators_and_states_survive_and_gate_sees_only_matching_financial_models(
    tmp_path, monkeypatch
):
    path = tmp_path / "runs"
    fixed = _seed(path)
    before = path.read_bytes()
    real_evaluate = integration.evaluate_financial_growth_gate
    calls = []

    def evaluate(ticker, *, models, metadata):
        assert metadata.generation_id == fixed.generation_id
        assert all(model.subject == ticker for model in models)
        assert {model.model_id for model in models} <= {"company_financials", "company_growth"}
        calls.append((ticker, len(models)))
        return real_evaluate(ticker, models=models, metadata=metadata)

    monkeypatch.setattr(integration, "evaluate_financial_growth_gate", evaluate)
    result = integration.inspect_technical_financial_candidates(
        generation_id=fixed.generation_id, runs_path=path
    )
    assert calls == [("1234", 2), ("2345", 2), ("3456", 1), ("6789", 0)]
    assert result["summary"] == {
        "planned_stocks": 6,
        "technical": {"matched": 4, "not_matched": 1, "unavailable": 1},
        "financial_gate": {
            "research_candidate": 1,
            "not_selected": 1,
            "input_unresolved": 2,
            "not_evaluated": 2,
        },
    }
    rows = {row["ticker"]: row for row in result["rows"]}
    assert tuple(rows) == _TICKERS
    assert len(rows["1234"]["technical"]["matched_routes"]) == 3
    assert rows["1234"]["financial_gate"]["financial_context"]["debt"] is None
    assert rows["2345"]["financial_gate"]["checks"]["positive_net_assets"]["value"] == -10.0
    assert rows["3456"]["financial_gate"]["missing_models"] == ["company_growth"]
    assert rows["6789"]["financial_gate"]["missing_models"] == [
        "company_financials",
        "company_growth",
    ]
    assert (
        rows["4567"]["financial_gate"]
        == rows["5678"]["financial_gate"]
        == {"state": "not_evaluated", "reason": "no_matched_technical_route"}
    )
    for ticker in ("1234", "2345", "3456", "6789"):
        assert (
            rows[ticker]["financial_gate"]["generation_id"]
            == rows[ticker]["technical"]["generation_id"]
        )
        assert "action" not in rows[ticker]["financial_gate"]
    assert result["as_of"] == "2026-11-14"
    assert rows["1234"]["technical"]["expected_price_date"] == "2026-11-13"
    assert result["adoption_status"] == "unadopted_research_only"
    assert "consumer_receipt_time_or_prospective_enrollment" in result["not_measured"]
    assert "screening_selection_id" not in result
    assert path.read_bytes() == before


@pytest.mark.parametrize("tickers", [_TICKERS, ()], ids=["no-matches", "empty-stock-plan"])
def test_zero_matches_does_not_read_or_apply_financial_models(tmp_path, monkeypatch, tickers):
    path = tmp_path / "runs"
    _seed(path, all_nonmatching=True, tickers=tickers)

    def forbidden(*args, **kwargs):
        pytest.fail("no matched TA route must not trigger a financial read or gate")

    monkeypatch.setattr(integration, "stream_generation_subjects", forbidden)
    monkeypatch.setattr(integration, "evaluate_financial_growth_gate", forbidden)
    result = integration.inspect_technical_financial_candidates(
        generation_id="technical-fixed", runs_path=path
    )
    assert result["summary"]["planned_stocks"] == len(tickers)
    assert result["summary"]["financial_gate"] == {
        "research_candidate": 0,
        "not_selected": 0,
        "input_unresolved": 0,
        "not_evaluated": len(tickers),
    }


def test_unknown_routes_remain_visible_when_an_independent_route_matches(tmp_path, monkeypatch):
    path = tmp_path / "runs"
    _seed(path)
    original = integration.inspect_technical_discovery

    def partial(**kwargs):
        result = deepcopy(original(**kwargs))
        row = result["rows"][0]
        row["routes"]["rebound_start"]["state"] = "unavailable"
        row["routes"]["rebound_start"]["missing_conditions"] = ["R1_positive"]
        return result

    monkeypatch.setattr(integration, "inspect_technical_discovery", partial)
    result = integration.inspect_technical_financial_candidates(
        generation_id="technical-fixed", runs_path=path
    )
    assert result["rows"][0]["financial_gate"]["state"] == "research_candidate"
    assert result["rows"][0]["technical"]["routes"]["rebound_start"]["state"] == "unavailable"


def test_mismatched_financial_generation_is_not_joined(tmp_path, monkeypatch):
    path = tmp_path / "runs"
    _seed(path)

    @contextmanager
    def mismatched(*args, **kwargs):
        with stream_generation_subjects(*args, **kwargs) as saved:
            metadata, stream = saved
            yield metadata.model_copy(update={"generation_id": "different-generation"}), stream

    monkeypatch.setattr(integration, "stream_generation_subjects", mismatched)
    out = tmp_path / "new.json"
    assert integration.main(_args(path, out)) == 2
    assert not out.exists()


@pytest.mark.parametrize(("ticker", "expected"), [("1234", 2), ("4567", 0)])
def test_only_matching_financial_payloads_are_read_and_failed_reads_never_publish_partial_output(
    tmp_path, ticker, expected
):
    path = tmp_path / "runs"
    _seed(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE generated_model SET payload_gzip=? WHERE subject=? AND model_id=?",
            (b"broken gzip", ticker, "company_growth"),
        )
    before = path.read_bytes()
    out = tmp_path / "new.json"
    assert integration.main(_args(path, out)) == expected
    assert out.exists() is (expected == 0)
    assert path.read_bytes() == before


def test_cli_repeats_exact_output_without_writing_sources_or_application(tmp_path):
    path = tmp_path / "runs"
    _seed(path)
    before = path.read_bytes()
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    assert integration.main(_args(path, first)) == integration.main(_args(path, second)) == 0
    assert first.read_bytes() == second.read_bytes()
    assert len(json.loads(first.read_text())["rows"]) == 6
    assert path.read_bytes() == before
    assert not (tmp_path / "isolated-db" / "app.sqlite").exists()


@pytest.mark.parametrize("symlink", [False, True])
def test_cli_does_not_overwrite_or_read_when_output_is_taken(tmp_path, monkeypatch, symlink):
    out = tmp_path / "result.json"
    if symlink:
        out.symlink_to(tmp_path / "absent-target")
    else:
        out.write_text("keep")

    def forbidden(**kwargs):
        pytest.fail("existing output must be rejected before reading")

    monkeypatch.setattr(integration, "inspect_technical_financial_candidates", forbidden)
    assert integration.main(_args(tmp_path / "absent-runs", out)) == 2
    if symlink:
        assert out.is_symlink()
        assert not out.exists()
    else:
        assert out.read_text() == "keep"


def test_missing_store_is_not_created_and_does_not_become_empty_success(tmp_path):
    path, out = tmp_path / "missing", tmp_path / "out.json"
    assert integration.main(_args(path, out)) == 2
    assert not path.exists()
    assert not out.exists()
