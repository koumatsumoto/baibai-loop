"""通常生成は一度だけ保存/投影し、旧saved経路と同じ全社比較を行う。"""

import sqlite3
import weakref
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime
from itertools import groupby
from types import SimpleNamespace

import pytest
from tests.engine.machine_analysis.generation.test_generation_assembly import generate
from tests.engine.machine_analysis.screening.test_economic_path_inputs import payloads
from tests.helpers.model_generation import publish_generation
from tests.helpers.trends import generation_inputs
from tests.jobs.integration.test_model_integration import integration as integration

import baibai_engine.machine_analysis.generation.query as generation_query
import baibai_engine.machine_analysis.screening.policies.economic_path_selection as screening
import baibai_jobs.integration.models as job
from baibai_engine.machine_analysis.generation.contracts import (
    GeneratedModel,
    GenerationSubject,
    ModelGeneration,
    ModelGenerationStream,
)
from baibai_engine.machine_analysis.generation.query import read_generation
from baibai_engine.machine_analysis.generation.store import (
    GenerationConflictError,
    ModelGenerationStore,
)
from baibai_engine.machine_analysis.screening.policies.economic_paths import EconomicPathPolicy
from baibai_engine.machine_analysis.screening.selection_query import read_publication
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore


def rows(path):
    with sqlite3.connect(path) as connection:
        return (
            connection.execute("SELECT * FROM model_generation ORDER BY generation_id").fetchall(),
            connection.execute(
                "SELECT * FROM generated_model ORDER BY generation_id,model_id,subject"
            ).fetchall(),
        )


def projected_stream(original, *, empty=False, schema_version=2):
    subjects = () if empty else ("1001", "1002", "1003", "9999")
    models = []
    plan = {}
    for ticker, price in zip(subjects[:3], (100.0, 200.0, 300.0), strict=False):
        data = payloads(price)
        data["company_growth"]["ticker"] = ticker
        if ticker == "1003":  # 部分生成とwhole-stock未生成を区別する。
            data = {"company_financials": data["company_financials"]}
        models.extend(
            GeneratedModel(model_id=name, subject=ticker, calculation_revision="v1", payload=body)
            for name, body in data.items()
        )
    for ticker in subjects:
        plan[ticker] = GenerationSubject(
            scope="stock",
            model_ids=("company_financials", "company_growth", "market_context"),
        )
    for subject, model_id in (("market", "market_context"), ("market:global", "social_trends")):
        plan[subject] = GenerationSubject(scope="market", model_ids=(model_id,))
        models.append(
            GeneratedModel(
                model_id=model_id,
                subject=subject,
                calculation_revision="shared-v1",
                payload={"read_semantics": "shared_not_a_company", "missing": None},
            )
        )
    metadata = original.metadata.model_copy(
        update={
            "schema_version": schema_version,
            "subject_plan": plan if schema_version == 2 else None,
        }
    )
    return ModelGenerationStream(metadata, subjects, iter(models)), tuple(models)


@pytest.mark.parametrize(("schema_version", "empty"), [(1, False), (2, False), (2, True)])
def test_normal_stream_matches_saved_oracle_without_decoding_models_again(
    integration, tmp_path, monkeypatch, schema_version, empty
):
    arguments, _ = integration
    arguments["policy"] = EconomicPathPolicy(candidate_limit=2)
    generate_models = job.read_and_generate_models
    expected = []
    visited = []
    project = job.project_company
    evaluate = job.evaluate_economic_coordinates

    def generate_stream(**kwargs):
        stream, models = projected_stream(
            generate_models(**kwargs), schema_version=schema_version, empty=empty
        )
        expected.append(ModelGeneration(**dict(stream.metadata), models=models))
        return stream

    def observe(ticker, **kwargs):
        visited.append(ticker)
        if ticker != "9999":
            with sqlite3.connect(arguments["runs"]) as connection:
                assert (
                    connection.execute("SELECT count(*) FROM model_generation").fetchone()[0] == 0
                )
        return project(ticker, **kwargs)

    def after_commit(metadata, **kwargs):
        with sqlite3.connect(arguments["runs"]) as connection:
            assert connection.execute("SELECT count(*) FROM model_generation").fetchone()[0] == 1
            assert connection.execute("SELECT count(*) FROM generated_model").fetchone()[0] == len(
                expected[0].models
            )
        return evaluate(metadata, **kwargs)

    def forbidden(*_args, **_kwargs):
        pytest.fail("normal integration reread its saved model payloads")

    monkeypatch.setattr(job, "read_and_generate_models", generate_stream)
    monkeypatch.setattr(job, "project_company", observe)
    monkeypatch.setattr(screening, "project_company", observe)
    monkeypatch.setattr(job, "evaluate_economic_coordinates", after_commit)
    with monkeypatch.context() as normal:
        normal.setattr(generation_query, "_decode_model", forbidden)
        normal.setattr(screening, "stream_generation_subjects", forbidden)
        result = job.integrate_models(**arguments)
    assert Counter(visited) == Counter(() if empty else ("1001", "1002", "1003", "9999"))
    stored = read_generation(result["generation_id"], path=arguments["runs"])
    assert stored == expected[0]
    oracle_path = tmp_path / "oracle.sqlite"
    publish_generation(expected[0], path=oracle_path)
    assert rows(arguments["runs"]) == rows(oracle_path)
    monkeypatch.setattr(screening, "project_company", project)
    oracle = screening.evaluate_saved_economic_generation(
        result["generation_id"],
        tickers=() if empty else ("1001", "1002", "1003", "9999"),
        policy=arguments["policy"],
        path=oracle_path,
    )
    publication = read_publication(path=arguments["runs"])
    assert publication.selection == oracle.selection
    assert result["screening_price_inputs"] == oracle.price_inputs
    ScreeningSelectionStore(oracle_path).publish(result["selection_id"], oracle.selection)
    with sqlite3.connect(arguments["runs"]) as current, sqlite3.connect(oracle_path) as baseline:
        sql = "SELECT payload_gzip FROM screening_selection WHERE selection_id=?"
        assert (
            current.execute(sql, (result["selection_id"],)).fetchone()
            == baseline.execute(sql, (result["selection_id"],)).fetchone()
        )
    if not empty:
        assert publication.selection.result.tickers == ("1001", "1002")
        assert publication.selection.universe_tickers == ("1001", "1002", "1003", "9999")
        assert (
            read_generation(result["generation_id"], path=arguments["runs"]).result(
                "company_financials", "1003"
            )
            is not None
        )
        assert publication.selection.result.cards[0].paths[0].price_population > 1


@pytest.mark.parametrize("failure", ["projection", "writer", "noncontiguous"])
def test_stream_failure_rolls_back_models_and_never_publishes_policy(
    integration, monkeypatch, failure
):
    arguments, events = integration
    generate_models = job.read_and_generate_models

    def generate_stream(**kwargs):
        stream, models = projected_stream(generate_models(**kwargs))
        if failure == "writer":
            models = (models[0], models[1].model_copy(update={"payload": {"bad": float("nan")}}))
        elif failure == "noncontiguous":
            models = (models[0], models[3], models[1], *models[4:])
        return replace(stream, models=iter(models))

    def forbidden(*_args, **_kwargs):
        pytest.fail("failed stream reached policy publication or pruning")

    monkeypatch.setattr(job, "read_and_generate_models", generate_stream)
    monkeypatch.setattr(job, "evaluate_economic_coordinates", forbidden)
    monkeypatch.setattr(job, "prune_model_cache", forbidden)
    if failure == "projection":

        def fail(*_args, **_kwargs):
            raise ValueError("projection failed")

        monkeypatch.setattr(job, "project_company", fail)
    message = {
        "projection": "projection failed",
        "writer": "Out of range float",
        "noncontiguous": "contiguous company models",
    }[failure]
    with pytest.raises(ValueError, match=message):
        job.integrate_models(**arguments)
    assert read_generation("models-new-release", path=arguments["runs"]) is None
    assert read_publication(path=arguments["runs"]) is None
    assert "publish" not in events


def test_retry_keeps_saved_bytes_and_conflict_stops_before_policy(integration, monkeypatch):
    arguments, _ = integration
    monkeypatch.setattr(
        job, "datetime", SimpleNamespace(now=lambda _: datetime(2026, 10, 26, tzinfo=UTC))
    )
    first = job.integrate_models(**arguments)
    before = rows(arguments["runs"])
    publication = read_publication(path=arguments["runs"])
    second = job.integrate_models(**arguments)
    assert rows(arguments["runs"]) == before
    assert read_publication(path=arguments["runs"]) == publication
    assert first["generation_id"] == second["generation_id"]
    generate_models = job.read_and_generate_models

    def changed(**kwargs):
        stream = generate_models(**kwargs)
        models = iter(stream.models)

        def changed_models():
            model = next(models)
            yield model.model_copy(update={"payload": {**model.payload, "changed": True}})
            yield from models

        return replace(stream, models=changed_models())

    def forbidden(*_args, **_kwargs):
        pytest.fail("conflicting saved identity reached policy publication")

    monkeypatch.setattr(job, "read_and_generate_models", changed)
    monkeypatch.setattr(job, "evaluate_economic_coordinates", forbidden)
    with pytest.raises(GenerationConflictError, match="different model content"):
        job.integrate_models(**arguments)
    assert rows(arguments["runs"]) == before
    assert read_publication(path=arguments["runs"]) == publication


def test_actual_assembly_emits_each_company_contiguously_before_shared_models():
    stream = generate(tickers=("1001", "1002"), stream=True, trends=generation_inputs())
    groups = [
        subject
        for subject, group in groupby(stream.models, key=lambda model: model.subject)
        if tuple(group)
    ]
    assert groups[:2] == list(stream.subjects)
    assert all(stream.metadata.subject_plan[subject].scope != "stock" for subject in groups[2:])


def test_full_models_are_released_as_companies_are_consumed(tmp_path):
    tickers = tuple(str(1000 + index) for index in range(12))
    stream = generate(tickers=tickers, stream=True)
    positions = {ticker: index for index, ticker in enumerate(tickers)}
    references = []
    coordinates = {}

    def observed():
        for model in stream.models:
            if model.subject in positions:
                assert all(
                    reference() is None
                    for subject, reference in references
                    if positions[subject] < positions[model.subject] - 1
                )
                references.append((model.subject, weakref.ref(model)))
            yield model

    tracked = replace(stream, models=observed())
    saved = ModelGenerationStore(tmp_path / "runs").publish_stream(
        replace(tracked, models=job._project_company_models(tracked, coordinates))
    )
    assert saved.model_count == len(tickers) * 5 + 1
    assert set(coordinates) == set(tickers)
    assert all(reference() is None for _, reference in references)
