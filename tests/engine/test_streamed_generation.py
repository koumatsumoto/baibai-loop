"""逐次生成は保存の原子性・不変identity・候補政策を変えない。"""

import sqlite3
from contextlib import closing

import pytest
from tests.engine.test_model_generation_store import generation

from baibai_engine.primitives.json import canonical_json
import baibai_engine.machine_analysis.generation.store as generation_store
from baibai_engine.machine_analysis.generation.store import (
    GenerationConflictError,
    ModelGenerationStore,
)
from baibai_engine.machine_analysis.generation.contracts import (
    GeneratedModel,
    GenerationMetadata,
    ModelGenerationStream,
)
from baibai_engine.machine_analysis.screening.model_projection import select_generation
from baibai_engine.machine_analysis.screening.saved_selection import select_saved_generation
from baibai_engine.machine_analysis.screening.views import TwoViewPolicy


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
    assert store.read(original.generation_id) == original
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
    store.publish(prior)
    original = generation()

    def rows():
        yield original.models[0]
        raise ValueError("late calculation failure")

    with pytest.raises(ValueError, match="late calculation"):
        store.publish_stream(stream(original, rows()))
    assert store.read(original.generation_id) is None
    assert store.read(prior.generation_id) == prior
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
    store.publish(original)
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
    assert store.read(original.generation_id) == original


def test_duplicate_fresh_stream_rolls_back(tmp_path):
    store = ModelGenerationStore(tmp_path / "runs.sqlite")
    original = generation()
    with pytest.raises(GenerationConflictError, match="duplicate"):
        store.publish_stream(stream(original, (original.models[0], original.models[0])))
    assert store.read(original.generation_id) is None


@pytest.mark.parametrize("earnings", [None, -0.1, 0.0, 0.100049])
def test_saved_selection_matches_pure_policy_without_decoding_unused_growth(tmp_path, earnings):
    path = tmp_path / "runs.sqlite"
    first = generation(value=earnings)
    first = type(first)(
        **first.model_dump(exclude={"models"}),
        models=tuple(
            model.model_copy(update={"payload": {"price_momentum": {"value": -0.5}}})
            if model.model_id == "technical_analysis"
            else model
            for model in first.models
        ),
    )
    extra = (
        GeneratedModel(
            model_id="technical_analysis",
            subject="5678",
            calculation_revision="v1",
            payload={"price_momentum": {"value": 0.5}},
        ),
        GeneratedModel(
            model_id="company_growth", subject="1234", calculation_revision="v1", payload={}
        ),
        first.models[0].model_copy(update={"subject": "5678"}),
    )
    original = type(first)(**first.model_dump(exclude={"models"}), models=(*first.models, *extra))
    ModelGenerationStore(path).publish(original)
    policy = TwoViewPolicy(earnings_limit=1, combined_limit=1)
    tickers = ("9999", "5678", "1234")
    expected = select_generation(original, tickers=tickers, policy=policy)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "UPDATE generated_model SET payload_gzip=? WHERE model_id='company_growth'",
            (b"not gzip",),
        )
        connection.commit()
    assert (
        select_saved_generation(original.generation_id, tickers=tickers, policy=policy, path=path)
        == expected
    )
    with pytest.raises(ValueError, match="not found"):
        select_saved_generation("absent", tickers=tickers, policy=policy, path=path)
