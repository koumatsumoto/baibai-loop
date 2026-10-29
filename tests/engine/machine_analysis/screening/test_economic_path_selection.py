import sqlite3
from contextlib import closing

import pytest
from tests.model_generation_fixtures import generation

from baibai_engine.machine_analysis.generation.contracts import GeneratedModel
from baibai_engine.machine_analysis.generation.store import ModelGenerationStore
from baibai_engine.machine_analysis.screening.contracts import ScreeningSelection
from baibai_engine.machine_analysis.screening.policies import economic_path_selection
from baibai_engine.machine_analysis.screening.policies.economic_paths import (
    CompanyCoordinates,
    EconomicPathPolicy,
    PathCoordinates,
    select_economic_paths,
)
from baibai_engine.storage.sqlite.machine_analysis import connect_read_only


@pytest.mark.parametrize("subjects", [("5678", "1234", "0000"), ()])
def test_reader_filters_universe_and_reads_only_fixed_generation_payloads(
    tmp_path, monkeypatch, subjects
):
    path = tmp_path / "runs.sqlite"
    models = (
        GeneratedModel(
            model_id="coordinate_fixture",
            subject="1234",
            calculation_revision="v1",
            payload={"price": 1.0, "business": 1.0},
        ),
        GeneratedModel(
            model_id="context_fixture",
            subject="1234",
            calculation_revision="v2",
            payload={"support": "not_an_extra_vote"},
        ),
        GeneratedModel(
            model_id="coordinate_fixture",
            subject="5678",
            calculation_revision="v1",
            payload={"price": 2.0, "business": 2.0},
        ),
        GeneratedModel(
            model_id="coordinate_fixture",
            subject="9999",
            calculation_revision="v1",
            payload={"price": 100.0, "business": 100.0},
        ),
    )
    fixed = generation().model_copy(update={"models": models})
    store = ModelGenerationStore(path)
    store.publish(fixed)
    other = fixed.model_copy(update={"generation_id": "other-generation"})
    store.publish(other)
    # 範囲外の企業・別世代を展開した場合はgzip読取が失敗するfixture。
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE generated_model SET payload_gzip=? WHERE subject='9999' OR generation_id=?",
            (b"not-gzip", other.generation_id),
        )

    projected = []

    def project_fixture(ticker, *, models, metadata):
        projected.append((ticker, tuple(model.model_id for model in models)))
        index = {model.model_id: model for model in models}
        coordinate = index.get("coordinate_fixture")
        paths = (
            ()
            if coordinate is None
            else (
                PathCoordinates(
                    path_id="business_expansion",
                    variant="quarter",
                    price_value=coordinate.payload["price"],
                    business_value=coordinate.payload["business"],
                    price_group="fixture",
                    business_group="fixture",
                    eligible=True,
                    evidence={},
                ),
            )
        )
        return CompanyCoordinates(
            ticker=ticker,
            name=None,
            sector="fixture",
            paths=paths,
            context={"generation_id": metadata.generation_id},
            model_revisions={model.model_id: model.calculation_revision for model in models},
            not_generated=(),
        )

    policy = EconomicPathPolicy()
    universe = tuple(sorted(subjects))
    expected = ScreeningSelection(
        generation_id=fixed.generation_id,
        universe_tickers=universe,
        result=select_economic_paths(
            tuple(
                project_fixture(
                    ticker,
                    models=tuple(
                        sorted(
                            (model for model in models if model.subject == ticker),
                            key=lambda model: model.model_id,
                        )
                    ),
                    metadata=fixed,
                )
                for ticker in universe
            ),
            policy=policy,
        ),
    )
    projected.clear()
    statements = []

    def traced_connection(path):
        connection = connect_read_only(path)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(economic_path_selection, "connect_read_only", traced_connection)
    monkeypatch.setattr(economic_path_selection, "project_company", project_fixture)
    actual = economic_path_selection.select_saved_economic_generation(
        fixed.generation_id,
        tickers=subjects,
        policy=policy,
        path=path,
    )
    assert actual == expected
    assert set(ticker for ticker, _ in projected) == set(subjects)
    if subjects:
        assert projected == [
            ("1234", ("context_fixture", "coordinate_fixture")),
            ("5678", ("coordinate_fixture",)),
            ("0000", ()),
        ]
        assert tuple(card.ticker for card in actual.result.cards) == ("5678", "1234")

    queries = [statement for statement in statements if "FROM generated_model" in statement]
    key_query = next(statement for statement in queries if "ORDER BY" in statement)
    assert key_query.startswith("SELECT subject,model_id FROM generated_model")
    assert "subject IN (SELECT value FROM json_each(" in key_query
    assert "payload_gzip" not in key_query
    payload_queries = [statement for statement in queries if "payload_gzip" in statement]
    assert len(payload_queries) == (3 if subjects else 0)
    assert all("ORDER BY" not in statement for statement in payload_queries)
    assert all(
        "WHERE generation_id='synthetic-generation' AND model_id=" in statement
        and " AND subject=" in statement
        for statement in payload_queries
    )
    assert statements[0] == "BEGIN"
