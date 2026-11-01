import sqlite3
from contextlib import closing

import pytest
from tests.helpers.model_generation import generation

from baibai_engine.machine_analysis.generation import query
from baibai_engine.machine_analysis.generation.contracts import GeneratedModel, GenerationMetadata
from baibai_engine.machine_analysis.generation.store import ModelGenerationStore
from baibai_engine.storage.sqlite.machine_analysis import connect_read_only


@pytest.mark.parametrize("subjects", [("5678", "1234", "0000"), ()])
def test_subject_stream_filters_fixed_generation_and_materializes_one_company_at_a_time(
    tmp_path, monkeypatch, subjects
):
    path = tmp_path / "runs.sqlite"
    fixed = generation()
    fixed = fixed.model_copy(
        update={
            "models": (
                *fixed.models,
                GeneratedModel(
                    model_id="company_financials",
                    subject="5678",
                    calculation_revision="v2",
                    payload={"earnings_price": {"earnings_yield": 0.2}},
                ),
                GeneratedModel(
                    model_id="company_financials",
                    subject="9999",
                    calculation_revision="v1",
                    payload={"earnings_price": {"earnings_yield": 100.0}},
                ),
            )
        }
    )
    store = ModelGenerationStore(path)
    store.publish(fixed)
    other = fixed.model_copy(update={"generation_id": "other-generation"})
    store.publish(other)
    # 範囲外の対象・別世代を展開した場合はgzip読取が失敗するfixture。
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE generated_model SET payload_gzip=? "
            "WHERE subject IN ('market','9999') OR generation_id=?",
            (b"not-gzip", other.generation_id),
        )

    connections = []
    statements = []

    def traced_connection(path):
        connection = connect_read_only(path)
        connection.set_trace_callback(statements.append)
        connections.append(connection)
        return connection

    monkeypatch.setattr(query, "connect_read_only", traced_connection)
    with query.stream_generation_subjects(
        fixed.generation_id, subjects=subjects, path=path
    ) as saved:
        metadata, companies = saved
        assert metadata == GenerationMetadata.model_validate(fixed.model_dump(exclude={"models"}))
        assert not any("payload_gzip" in statement for statement in statements)
        if subjects:
            assert next(companies) == (
                "1234",
                tuple(model for model in fixed.models if model.subject == "1234"),
            )
            # 次社のpayloadは、その社を消費するまで読まない。
            assert not any("subject='5678'" in statement for statement in statements)
            assert next(companies) == (
                "5678",
                tuple(model for model in fixed.models if model.subject == "5678"),
            )
        assert tuple(companies) == ()
        assert len(connections) == 1
        assert connections[0].in_transaction

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
    assert statements.count("BEGIN") == 1
    assert statements[0] == "BEGIN"
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")


def test_subject_stream_closes_when_consumer_stops_before_the_next_company(tmp_path, monkeypatch):
    path = tmp_path / "runs.sqlite"
    fixed = generation()
    ModelGenerationStore(path).publish(fixed)
    connection = connect_read_only(path)
    monkeypatch.setattr(query, "connect_read_only", lambda _path: connection)

    streams = []

    def consume_and_fail():
        with query.stream_generation_subjects(
            fixed.generation_id, subjects=("1234", "market"), path=path
        ) as saved:
            _, companies = saved
            streams.append(companies)
            assert next(companies)[0] == "1234"
            raise ValueError("projection failed")

    with pytest.raises(ValueError, match="projection failed"):
        consume_and_fail()

    assert next(streams[0], None) is None
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
