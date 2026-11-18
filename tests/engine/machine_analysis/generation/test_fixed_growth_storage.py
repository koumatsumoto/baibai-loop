from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256

import pytest

from baibai_engine.machine_analysis.generation.contracts import (
    GeneratedModel,
    GenerationMetadata,
    InputSnapshot,
    ModelGenerationStream,
)
from baibai_engine.machine_analysis.generation.query import read_model_inputs
from baibai_engine.machine_analysis.generation.store import ModelGenerationStore, delete_generations
from baibai_engine.primitives.json import encode_json
from baibai_engine.storage.sqlite.machine_analysis import connect_rw


def _data():
    return {
        "originals": [sha256(str(n).encode()).hexdigest() for n in range(220)],
        "facts": [None, 0, False, -0.0],
    }


def _publish(path, generation, sequence, payload, *, revision="growth-v1", issuer="a"):
    meta = GenerationMetadata(
        generation_id=generation,
        evaluation_time=datetime(2026, 11, 18, tzinfo=UTC) + timedelta(microseconds=sequence),
        as_of=date(2026, 11, 18),
        inputs={
            "financial": InputSnapshot(kind="lake_release", reference="finite", sha256="a" * 64)
        },
    )
    model = GeneratedModel(
        model_id="company_growth", calculation_revision=revision, subject=issuer, payload=payload
    )
    return ModelGenerationStore(path).publish_stream(ModelGenerationStream(meta, (), iter([model])))


def _blobs(path):
    with closing(connect_rw(path)) as connection:
        return dict(connection.execute("SELECT generation_id,payload_gzip FROM generated_model"))


def _read(path, generation):
    return encode_json(read_model_inputs(generation, subjects=("a",), path=path).models[0].payload)


def test_growth_reuses_fixed_bytes_without_turning_missing_frame_into_null(tmp_path):
    path = tmp_path / "runs"
    full = _data()
    cases = {
        "source": full,
        "first": full | {"requested_end": None},
        "second": full | {"requested_end": None, "input_release": {"receipt": "new"}},
        "third": full | {"requested_end": "2026-11-18"},
    }
    for i, (identity, value) in enumerate(cases.items()):
        assert _publish(path, identity, i, value).created
        assert not _publish(path, identity, i, value).created
    before = _blobs(path)
    assert all(len(before[k]) + 4096 < len(before["source"]) for k in list(cases)[1:])
    with closing(connect_rw(path)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        delete_generations(connection, ["source"])
    after = _blobs(path)
    assert len(after["first"]) > 4096
    for k in list(cases)[1:]:
        assert _read(path, k) == encode_json(cases[k])
    assert all(len(after[k]) < len(after["first"]) for k in ("second", "third"))


@pytest.mark.parametrize(
    "changed",
    [
        {"facts": [None, 0.0, False, -0.0]},
        {"facts": [None, 0, False, 0.0]},
        {"facts": [None, False, 0, -0.0]},
        {"source_id": None},
        {"source_id": "corrected"},
        {"requested_start": "2025-11-18"},
    ],
)
def test_any_nonframe_canonical_change_remains_an_independent_original(tmp_path, changed):
    path = tmp_path / "runs"
    for i, (identity, value) in enumerate((("first", _data()), ("next", _data() | changed))):
        _publish(path, identity, i, value)
    assert all(blob.startswith(b"\x1f\x8b") for blob in _blobs(path).values())
    assert _read(path, "next") == encode_json(_data() | changed)


def test_no_new_body_for_the_wrong_clock_revision_or_issuer_and_tiny_is_standalone(tmp_path):
    path = tmp_path / "runs"
    for identity, clock, revision, issuer in (
        ("future", 8, "one", "a"),
        ("earlier", 7, "one", "a"),
        ("grade", 9, "two", "a"),
        ("other", 10, "two", "b"),
    ):
        _publish(path, identity, clock, _data(), revision=revision, issuer=issuer)
    now = _blobs(path)
    assert all(blob.startswith(b"\x1f\x8b") for blob in now.values())
    for i in (11, 12):
        _publish(path, str(i), i, {"status": "unavailable", "ticker": "b"}, issuer="b")
    assert _blobs(path)["11"] == _blobs(path)["12"]


def test_damaged_prior_does_not_invalidate_new_independent_value(tmp_path, caplog):
    path = tmp_path / "runs"
    _publish(path, "original", 0, _data())
    _publish(path, "linked", 1, _data() | {"requested_end": "2026-11-18"})
    with closing(connect_rw(path)) as connection:
        connection.execute("DELETE FROM generated_model WHERE generation_id='original'")
    with pytest.raises(ValueError, match="missing or changed"):
        _read(path, "linked")
    new = _data() | {"source_id": "newly-received"}
    _publish(path, "independent", 2, new)
    assert _read(path, "independent") == encode_json(new)
    assert "storing independent current result" in caplog.text
    assert _blobs(path)["independent"].startswith(b"\x1f\x8b")
    with pytest.raises(ValueError, match="missing or changed"):
        _read(path, "linked")


def test_frame_hint_does_not_alias_changed_or_nested_frame_fields(tmp_path):
    path = tmp_path / "runs"
    constant = _data() | {"nested": {"requested_end": "historical-value", "input_release": None}}
    for i, frame in enumerate(({}, {"input_release": 3}, {"input_release": False})):
        value = constant | frame
        _publish(path, str(i), i, value)
        assert _read(path, str(i)) == encode_json(value)


def test_first_reference_marks_store_only_when_its_generation_commits(tmp_path):
    path = tmp_path / "runs"

    def version():
        with closing(connect_rw(path)) as connection:
            return connection.execute("PRAGMA user_version").fetchone()[0]

    _publish(path, "source", 0, _data())
    assert version() == 8
    hdr = GenerationMetadata(
        generation_id="interrupted",
        as_of=date(2026, 11, 18),
        evaluation_time=datetime(2026, 11, 19, tzinfo=UTC),
        inputs={
            "financial": InputSnapshot(kind="lake_release", reference="fixed", sha256="f" * 64)
        },
    )

    def interrupted():
        yield GeneratedModel(
            model_id="company_growth",
            subject="a",
            calculation_revision="growth-v1",
            payload=_data() | {"requested_end": "today"},
        )
        raise RuntimeError("producer stopped after first linked row")

    with pytest.raises(RuntimeError, match="producer stopped"):
        ModelGenerationStore(path).publish_stream(ModelGenerationStream(hdr, (), interrupted()))
    assert version() == 8
    assert set(_blobs(path)) == {"source"}
    _publish(path, "committed", 1, _data() | {"requested_end": "today"})
    assert version() == 9
