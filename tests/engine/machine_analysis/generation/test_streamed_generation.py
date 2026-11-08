"""逐次生成は保存の原子性・不変identity・候補政策を変えない。"""

import sqlite3
from contextlib import closing

import pytest
from tests.helpers.model_generation import generation, materialize_generation, publish_generation

import baibai_engine.machine_analysis.generation.store as generation_store
from baibai_engine.machine_analysis.generation.contracts import (
    GenerationMetadata,
    GenerationSubject,
    ModelGenerationStream,
)
from baibai_engine.machine_analysis.generation.query import read_generation
from baibai_engine.machine_analysis.generation.store import (
    GenerationConflictError,
    ModelGenerationStore,
)
from baibai_engine.primitives.json import canonical_json


def stream(original, models=None):
    return ModelGenerationStream(
        GenerationMetadata.model_validate(original.model_dump(exclude={"models"})),
        ("1234", "5678"),
        iter(original.models if models is None else models),
    )


def test_stream_consumed_once_and_encoded_before_next_row_without_partial_visibility(
    tmp_path, monkeypatch
):
    path = tmp_path / "runs.sqlite"
    store = ModelGenerationStore(path)
    original = generation()
    encoded = []
    compress = generation_store.gzip.compress

    def record(payload, **kwargs):
        encoded.append(payload)
        return compress(payload, **kwargs)

    monkeypatch.setattr(generation_store.gzip, "compress", record)

    def rows():
        for index, model in enumerate(original.models):
            assert len(encoded) == index
            with closing(sqlite3.connect(path)) as reader:
                assert reader.execute("SELECT count(*) FROM model_generation").fetchone()[0] == 0
            yield model

    result = store.publish_stream(stream(original, rows()))
    assert result.created
    assert result.model_count == len(original.models)
    assert read_generation(original.generation_id, path=path) == original
    with closing(sqlite3.connect(path)) as reader:
        assert reader.execute("SELECT header FROM model_generation").fetchone()[
            0
        ] == canonical_json(original.model_dump(mode="json", exclude={"models"}))
    retry = store.publish_stream(stream(original, reversed(original.models)))
    assert not retry.created
    assert retry.model_count == len(original.models)


def test_late_generator_failure_rolls_back_all_rows_and_preserves_previous_generation(tmp_path):
    path = tmp_path / "runs.sqlite"
    store = ModelGenerationStore(path)
    prior = generation().model_copy(update={"generation_id": "prior"})
    publish_generation(prior, path=path)
    original = generation()

    def rows():
        yield original.models[0]
        raise ValueError("late calculation failure")

    with pytest.raises(ValueError, match="late calculation"):
        store.publish_stream(stream(original, rows()))
    assert read_generation(original.generation_id, path=path) is None
    assert read_generation(prior.generation_id, path=path) == prior
    with closing(sqlite3.connect(path)) as reader:
        assert reader.execute("SELECT count(*) FROM generated_model").fetchone()[0] == len(
            prior.models
        )


@pytest.mark.parametrize(
    "change", ["header", "revision", "payload", "missing", "extra", "duplicate"]
)
def test_stream_retry_cannot_hide_changed_or_missing_content(tmp_path, change):
    store = ModelGenerationStore(tmp_path / "runs.sqlite")
    original = generation()
    publish_generation(original, path=tmp_path / "runs.sqlite")
    changed = original
    models = list(original.models)
    if change == "header":
        changed = original.model_copy(update={"as_of": original.as_of.replace(day=22)})
    elif change == "revision":
        models[0] = models[0].model_copy(update={"calculation_revision": "v2"})
    elif change == "payload":
        models[0] = models[0].model_copy(update={"payload": {"changed": True}})
    elif change == "missing":
        models.pop()
    elif change == "extra":
        models.append(models[0].model_copy(update={"subject": "9999"}))
    else:
        models[-1] = models[0]
    with pytest.raises(GenerationConflictError):
        store.publish_stream(stream(changed, models))
    assert read_generation(original.generation_id, path=tmp_path / "runs.sqlite") == original


def test_duplicate_fresh_stream_rolls_back(tmp_path):
    store = ModelGenerationStore(tmp_path / "runs.sqlite")
    original = generation()
    with pytest.raises(GenerationConflictError, match="duplicate"):
        store.publish_stream(stream(original, (original.models[0], original.models[0])))
    assert read_generation(original.generation_id, path=tmp_path / "runs.sqlite") is None


def test_nonfinite_last_streamed_model_rolls_back_header_and_previous_rows(tmp_path):
    path = tmp_path / "runs.sqlite"
    original = generation(value=float("nan"))
    consumed = []

    def rows():
        for model in reversed(original.models):
            consumed.append(model.model_id)
            yield model

    with pytest.raises(ValueError, match="Out of range float"):
        ModelGenerationStore(path).publish_stream(stream(original, rows()))
    assert consumed == ["technical_analysis", "market_context", "company_financials"]
    assert read_generation(original.generation_id, path=path) is None
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT count(*) FROM generated_model").fetchone()[0] == 0


def test_stream_rejects_incomplete_fixed_plan_before_consuming_models(tmp_path):
    path = tmp_path / "runs.sqlite"
    original = generation()
    metadata = GenerationMetadata.model_validate(
        {
            **original.model_dump(exclude={"models"}),
            "schema_version": 2,
            "subject_plan": {
                "1234": GenerationSubject(
                    scope="stock",
                    model_ids=("company_financials", "technical_analysis"),
                    related_subjects=("sector",),
                )
            },
        }
    )

    def rows():
        raise AssertionError("rejected metadata must not consume model results")
        yield from original.models

    with pytest.raises(ValueError, match="related subject"):
        ModelGenerationStore(path).publish_stream(
            ModelGenerationStream(metadata, ("1234",), rows())
        )
    assert not path.exists()


def test_finite_fixture_materialization_preserves_inputs_clock_and_single_pass():
    original = generation()
    consumed = []

    def rows():
        for model in reversed(original.models):
            consumed.append(model)
            yield model

    source = stream(original, rows())
    assert materialize_generation(source) == original
    assert consumed == list(reversed(original.models))
    assert tuple(source.models) == ()
    assert source.subjects == ("1234", "5678")


def test_finite_fixture_materialization_propagates_iterator_failure():
    original = generation()

    def rows():
        yield original.models[0]
        raise ValueError("late fixture calculation failure")

    with pytest.raises(ValueError, match="late fixture calculation failure"):
        materialize_generation(stream(original, rows()))
