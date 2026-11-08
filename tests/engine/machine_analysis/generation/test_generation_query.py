import sqlite3
from contextlib import closing

import pytest
from tests.helpers.model_generation import generation, publish_generation

from baibai_engine.machine_analysis.generation import query
from baibai_engine.machine_analysis.generation.contracts import (
    GeneratedModel,
    GenerationMetadata,
    GenerationSubject,
    ModelGeneration,
)
from baibai_engine.machine_analysis.screening.policies.economic_path_selection import (
    evaluate_saved_economic_generation,
)
from baibai_engine.machine_analysis.screening.policies.economic_paths import EconomicPathPolicy
from baibai_engine.primitives.json import canonical_json
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
    publish_generation(fixed, path=path)
    other = fixed.model_copy(update={"generation_id": "other-generation"})
    publish_generation(other, path=path)
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
    publish_generation(fixed, path=path)
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


@pytest.mark.parametrize("model_ids", [("company_financials",), ()])
@pytest.mark.parametrize("schema_version", [1, 2])
def test_subject_stream_filters_model_keys_before_decoding(tmp_path, model_ids, schema_version):
    path = tmp_path / "runs.sqlite"
    fixed = generation()
    if schema_version == 2:
        fixed = ModelGeneration(
            **fixed.model_dump(exclude={"schema_version"}),
            schema_version=2,
            subject_plan={
                "1234": GenerationSubject(
                    scope="stock", model_ids=("company_financials", "technical_analysis")
                ),
                "market": GenerationSubject(scope="market", model_ids=("market_context",)),
            },
        )
    publish_generation(fixed, path=path)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE generated_model SET payload_gzip=? WHERE model_id!='company_financials'",
            (b"not-gzip",),
        )
    before = path.read_bytes()
    with query.stream_generation_subjects(
        fixed.generation_id, subjects=("1234", "market"), model_ids=model_ids, path=path
    ) as saved:
        _, companies = saved
        assert tuple(companies) == (
            (("1234", (fixed.result("company_financials", "1234"),)),) if model_ids else ()
        )
    assert path.read_bytes() == before


def test_subject_stream_freezes_requested_scope_before_lazy_consumption(tmp_path):
    path = tmp_path / "runs.sqlite"
    fixed = generation()
    publish_generation(fixed, path=path)
    subjects, model_ids = ["1234"], ["company_financials"]
    with query.stream_generation_subjects(
        fixed.generation_id, subjects=subjects, model_ids=model_ids, path=path
    ) as saved:
        subjects.append("market")
        model_ids.append("technical_analysis")
        _, companies = saved
        assert tuple(companies) == (("1234", (fixed.result("company_financials", "1234"),)),)


@pytest.mark.parametrize("invalid", ["subject", "model"])
def test_subject_stream_rejects_selected_models_outside_the_saved_plan(tmp_path, invalid):
    path = tmp_path / "runs.sqlite"
    fixed = generation()
    publish_generation(fixed, path=path)
    plan = {"market": GenerationSubject(scope="market", model_ids=("market_context",))}
    if invalid == "model":
        plan["1234"] = GenerationSubject(scope="stock", model_ids=("technical_analysis",))
    metadata = GenerationMetadata(
        **fixed.model_dump(exclude={"models", "schema_version"}),
        schema_version=2,
        subject_plan=plan,
    )
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE model_generation SET header=? WHERE generation_id=?",
            (canonical_json(metadata.model_dump(mode="json")), fixed.generation_id),
        )
    before = path.read_bytes()
    with pytest.raises(ValueError, match="subject plan"):
        query.read_generation(fixed.generation_id, path=path)
    with pytest.raises(ValueError, match="subject plan"):
        query.read_model_inputs(fixed.generation_id, subjects=("1234",), path=path)
    with (
        pytest.raises(ValueError, match="subject plan"),
        query.stream_generation_subjects(
            fixed.generation_id,
            subjects=("1234",),
            model_ids=("company_financials",),
            path=path,
        ) as saved,
    ):
        tuple(saved[1])
    with pytest.raises(ValueError, match="subject plan"):
        evaluate_saved_economic_generation(
            fixed.generation_id, tickers=("1234",), policy=EconomicPathPolicy(), path=path
        )
    assert path.read_bytes() == before


@pytest.mark.parametrize("schema_version", [2, 99])
def test_subject_stream_rejects_invalid_saved_header_before_models(tmp_path, schema_version):
    path = tmp_path / "runs.sqlite"
    fixed = generation()
    publish_generation(fixed, path=path)
    header = fixed.model_dump(mode="json", exclude={"models"})
    header["schema_version"] = schema_version
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE model_generation SET header=? WHERE generation_id=?",
            (canonical_json(header), fixed.generation_id),
        )
    before = path.read_bytes()
    with (
        pytest.raises(ValueError, match=r"subject plan|schema_version"),
        query.stream_generation_subjects(
            fixed.generation_id, subjects=("1234",), path=path
        ) as saved,
    ):
        tuple(saved[1])
    assert path.read_bytes() == before
