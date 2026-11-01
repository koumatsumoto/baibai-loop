import gzip
import sqlite3
from contextlib import closing

import pytest
from tests.helpers.model_generation import generation

from baibai_engine.machine_analysis.generation.contracts import (
    GeneratedModel,
    InputSnapshot,
    ModelGeneration,
)
from baibai_engine.machine_analysis.generation.store import (
    GenerationConflictError,
    ModelGenerationStore,
)
from baibai_engine.storage.sqlite.machine_analysis import (
    GENERATION_SCHEMA_SQL,
    SCHEMA_SQL,
    connect_read_only,
    initialize_run_store,
)


def test_generation_round_trip_is_independent_of_candidate_selection(tmp_path):
    store = ModelGenerationStore(tmp_path / "runs.sqlite")
    original = generation()
    assert store.publish(original)
    assert not store.publish(original)
    assert store.read(original.generation_id) == original
    assert store.read("missing") is None
    company = store.read_subjects(original.generation_id, subjects=("1234",))
    assert {model.subject for model in company.models} == {"1234"}
    assert company.inputs == original.inputs
    combined = store.read_subjects(original.generation_id, subjects=("1234", "market"))
    assert combined.models == original.models
    assert combined.subjects == ("1234", "market")
    assert store.read_subjects(original.generation_id, subjects=()).models == ()


def test_subject_slice_does_not_decode_unselected_payloads(tmp_path):
    path = tmp_path / "runs.sqlite"
    store = ModelGenerationStore(path)
    original = generation()
    store.publish(original)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE generated_model SET payload_gzip=? WHERE generation_id=? AND subject='market'",
            (b"not gzip", original.generation_id),
        )
    company = store.read_subjects(original.generation_id, subjects=("1234", "1234"))
    assert company.subjects == ("1234",)
    assert company.models == tuple(model for model in original.models if model.subject == "1234")
    with pytest.raises(gzip.BadGzipFile):
        store.read(original.generation_id)


def test_partial_input_cannot_be_published_as_a_complete_generation(tmp_path):
    store = ModelGenerationStore(tmp_path / "runs.sqlite")
    first = generation()
    second_company = GeneratedModel(
        model_id="company_financials",
        subject="5678",
        calculation_revision="v1",
        payload={"earnings_price": {"earnings_yield": 0.2}},
    )
    full = ModelGeneration.model_validate(
        {**first.model_dump(), "models": (*first.models, second_company)}
    )
    store.publish(full)
    partial = store.read_subjects(full.generation_id, subjects=("1234",))
    with pytest.raises(TypeError, match="complete model generation"):
        store.publish(partial)
    assert store.read(full.generation_id) == full


def test_generation_identity_cannot_be_reused_for_new_values_or_new_inputs(tmp_path):
    store = ModelGenerationStore(tmp_path / "runs.sqlite")
    original = generation()
    store.publish(original)
    with pytest.raises(GenerationConflictError, match="model content"):
        store.publish(generation(value=0.100041))
    different_input = original.model_copy(
        update={
            "inputs": {
                "market": InputSnapshot(
                    kind="lake_release", reference="different-input", sha256="b" * 64
                )
            }
        }
    )
    with pytest.raises(GenerationConflictError, match="different content"):
        store.publish(different_input)
    assert store.read(original.generation_id) == original


def test_failed_generation_publication_rolls_back_header_and_models(tmp_path):
    path = tmp_path / "runs.sqlite"
    store = ModelGenerationStore(path)
    with pytest.raises(ValueError, match="Out of range float"):
        store.publish(generation(value=float("nan")))
    assert store.read("synthetic-generation") is None
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT count(*) FROM generated_model").fetchone()[0] == 0


def test_schema6_is_rejected_without_changing_existing_data(tmp_path):
    path = tmp_path / "runs.sqlite"
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(SCHEMA_SQL.removesuffix(GENERATION_SCHEMA_SQL))
        connection.execute("PRAGMA user_version=6")
        connection.execute(
            "INSERT INTO screening_run VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "old",
                "old",
                "2026-10-23",
                "2026-10-23",
                "2026-10-23T00:00:00Z",
                0,
                None,
                "old",
                "{}",
            ),
        )
        connection.commit()
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="obsolete schema"):
        initialize_run_store(path)
    with pytest.raises(RuntimeError, match="schema is not current"):
        connect_read_only(path)
    assert path.read_bytes() == before
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT payload FROM screening_run").fetchone()[0] == "{}"
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
        assert (
            connection.execute(
                "SELECT name FROM sqlite_schema WHERE name = 'model_generation'"
            ).fetchone()
            is None
        )


def test_streamed_subject_plan_rejects_unplanned_model_without_partial_publication(tmp_path):
    from baibai_engine.machine_analysis.generation.contracts import (
        GenerationMetadata,
        GenerationSubject,
        ModelGenerationStream,
    )

    path = tmp_path / "planned.sqlite"
    original = generation()
    metadata = GenerationMetadata.model_validate(
        {
            **original.model_dump(exclude={"models"}),
            "schema_version": 2,
            "subject_plan": {
                "1234": GenerationSubject(scope="stock", model_ids=("allowed_probe",))
            },
        }
    )
    stream = ModelGenerationStream(metadata, ("1234",), iter(original.models))
    store = ModelGenerationStore(path)
    with pytest.raises(ValueError, match="subject plan"):
        store.publish_stream(stream)
    assert store.read(original.generation_id) is None
