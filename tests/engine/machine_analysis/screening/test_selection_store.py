"""候補政策の保存・読取契約。モデル生成の保存testとは責務を分ける。"""

import gzip
import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, date, datetime

import pytest
from pydantic import TypeAdapter, ValidationError
from tests.helpers.model_generation import empty_selection, generation, publish_generation

from baibai_engine.machine_analysis.screening.contracts import ScreeningSelection
from baibai_engine.machine_analysis.screening.policies.economic_paths import (
    EconomicPathPolicy,
    EconomicPathSelection,
)
from baibai_engine.machine_analysis.screening.policies.technical_financial import (
    TechnicalFinancialPolicy,
    TechnicalFinancialSelection,
)
from baibai_engine.machine_analysis.screening.policies.two_views import (
    CandidateView,
    RankedCompany,
    TwoViewPolicy,
    ViewSelection,
)
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore
from baibai_engine.primitives.json import canonical_json


def _empty_result(policy):
    if isinstance(policy, TechnicalFinancialPolicy):
        return TechnicalFinancialSelection(policy, "synthetic-t", "synthetic-g", (), (), {})
    if isinstance(policy, EconomicPathPolicy):
        return EconomicPathSelection(policy, 0, ())
    return ViewSelection(policy, ())


@pytest.mark.parametrize(
    ("policy", "update"),
    [
        (TechnicalFinancialPolicy(), {"page_size": 0}),
        (TechnicalFinancialPolicy(), {"page_size": 37}),
        (TechnicalFinancialPolicy(), {"page_size": "36"}),
        (TechnicalFinancialPolicy(), {"method": "unknown-policy"}),
        (EconomicPathPolicy(), {"candidate_limit": -1}),
        (EconomicPathPolicy(), {"candidate_limit": 37}),
        (TwoViewPolicy(earnings_limit=1, combined_limit=1), {"earnings_limit": -1}),
        (TwoViewPolicy(earnings_limit=1, combined_limit=1), {"earnings_scale": 0.0}),
        (TwoViewPolicy(earnings_limit=1, combined_limit=1), {"earnings_weight": 1.1}),
    ],
)
def test_invalid_policy_copy_is_rejected_before_any_store_write(
    tmp_path, monkeypatch, policy, update
):
    from baibai_engine.machine_analysis.screening.selection_query import read_publication

    path = tmp_path / "runs.sqlite"
    source = generation()
    publish_generation(source, path=path)
    selected = ScreeningSelection(source.generation_id, ("1234",), _empty_result(policy))
    store = ScreeningSelectionStore(path)
    assert store.publish("valid", selected)
    first = read_publication(path=path)
    before = path.read_bytes()
    invalid = replace(
        selected, result=replace(selected.result, policy=policy.model_copy(update=update))
    )

    def forbidden(*_args, **_kwargs):
        pytest.fail("invalid policy must be rejected before store initialization")

    uninitialized = tmp_path / "uninitialized.sqlite"
    with monkeypatch.context() as patch:
        patch.setattr(
            "baibai_engine.machine_analysis.screening.selection_store.initialize_run_store",
            forbidden,
        )
        with pytest.raises(ValidationError):
            store.publish("rejected", invalid)
        with pytest.raises(ValidationError):
            ScreeningSelectionStore(uninitialized).publish("rejected", invalid)
    assert not uninitialized.exists()
    assert path.read_bytes() == before
    assert read_publication(path=path) == first
    assert store.read("valid") == selected
    assert store.read("rejected") is None
    assert not store.publish("valid", selected)
    assert read_publication(path=path) == first
    assert store.publish("rejected", selected)
    assert read_publication(path=path).selection_id == "rejected"
    assert store.read("rejected") == selected


@pytest.mark.parametrize(
    "policy",
    [
        TwoViewPolicy(earnings_limit=2, combined_limit=3),
        *(EconomicPathPolicy(method=f"best-economic-path-v{version}") for version in range(1, 5)),
        *(
            TechnicalFinancialPolicy(method=f"technical-financial-discovery-v{version}")
            for version in range(1, 5)
        ),
    ],
)
def test_valid_policy_copy_revalidates_only_settings_and_preserves_saved_bytes(
    tmp_path, monkeypatch, policy
):
    import baibai_engine.machine_analysis.screening.selection_store as storage
    from baibai_engine.machine_analysis.screening.selection_query import read_publication

    path = tmp_path / "runs.sqlite"
    source = generation()
    publish_generation(source, path=path)
    settings = policy.model_dump(mode="python")
    copied = policy.model_copy(update=settings)
    selected = ScreeningSelection(
        source.generation_id,
        ("1234",),
        _empty_result(copied),
        price_inputs={"independent_context": {"observations": [1, None, 0.0]}},
    )
    expected = canonical_json(TypeAdapter(ScreeningSelection).dump_python(selected, mode="json"))
    validate_policy = type(policy).model_validate
    validated = []

    def validate_settings(cls, value, **kwargs):
        validated.append(value)
        assert value == settings
        return validate_policy(value, **kwargs)

    def forbidden(*_args, **_kwargs):
        pytest.fail("saving must not reconstruct the full company snapshot for policy validation")

    monkeypatch.setattr(type(policy), "model_validate", classmethod(validate_settings))
    monkeypatch.setattr(storage._CODEC, "validate_python", forbidden)
    monkeypatch.setattr(storage._CODEC, "validate_json", forbidden)
    store = ScreeningSelectionStore(path)
    assert store.publish("fixed", selected)
    first = read_publication(path=path)
    with sqlite3.connect(path) as connection:
        blob = connection.execute("SELECT payload_gzip FROM screening_selection").fetchone()[0]
    assert blob == gzip.compress(expected.encode(), mtime=0)
    before = path.read_bytes()
    assert not store.publish("fixed", selected)
    assert validated == [settings, settings]
    assert path.read_bytes() == before
    assert read_publication(path=path) == first
    assert store.read("fixed") == selected


def test_selection_round_trip_preserves_policy_universe_and_unrounded_result(tmp_path, monkeypatch):
    path = tmp_path / "runs.sqlite"
    original = generation()
    publish_generation(original, path=path)
    selected = ScreeningSelection(
        original.generation_id,
        ("1234", "5678"),
        ViewSelection(
            TwoViewPolicy(earnings_limit=2, combined_limit=2),
            (
                CandidateView("earnings-price", 2, 1, (RankedCompany("1234", 1, 0.100049),)),
                CandidateView("earnings-price-response", 2, 0, ()),
            ),
        ),
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("storage must not execute candidate policy")

    monkeypatch.setattr(
        "baibai_engine.machine_analysis.screening.policies.economic_path_selection.evaluate_economic_coordinates",
        forbidden,
    )
    monkeypatch.setattr(
        "baibai_engine.machine_analysis.screening.policies.economic_paths.select_economic_paths",
        forbidden,
    )
    store = ScreeningSelectionStore(path)
    assert store.publish("selection-a", selected)
    assert not store.publish("selection-a", selected)
    restored = store.read("selection-a")
    assert restored == selected
    assert restored.universe_tickers == ("1234", "5678")
    assert restored.result.views[0].companies[0].ranking_value == 0.100049
    assert restored.result.views[1].companies == ()
    assert store.read("missing") is None


def test_selection_cannot_reference_an_unpublished_generation(tmp_path):
    selected = empty_selection(generation().generation_id)
    store = ScreeningSelectionStore(tmp_path / "runs.sqlite")
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        store.publish("selection-a", selected)
    assert store.read("selection-a") is None


def test_publication_order_prefers_asof_then_real_publication_and_preserves_retry(tmp_path):
    from baibai_engine.machine_analysis.screening.selection_query import read_publication

    path = tmp_path / "runs"
    store = ScreeningSelectionStore(path)
    current = generation()
    older = current.model_copy(update={"generation_id": "older", "as_of": date(2026, 10, 22)})
    publish_generation(current, path=path)
    publish_generation(older, path=path)
    for name, source, limit in (
        ("z-first", current, 1),
        ("a-second", current, 2),
        ("late-backfill", older, 1),
    ):
        store.publish(
            name,
            empty_selection(source.generation_id, candidate_limit=limit),
        )
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executemany(
            "UPDATE screening_selection SET published_at=? WHERE selection_id=?",
            [
                ("2026-10-25T09:00:00+00:00", "z-first"),
                ("2026-10-25T18:00:01+09:00", "a-second"),
                ("2026-10-26T00:00:00+00:00", "late-backfill"),
            ],
        )
    latest = read_publication(path=path)
    assert latest.selection_id == "a-second"
    cutoff = datetime(2026, 10, 25, 9, tzinfo=UTC)
    assert read_publication(path=path, known_at=cutoff).selection_id == "z-first"
    assert read_publication(path=path, selection_id="a-second", known_at=cutoff) is None
    assert latest.selection.result.policy.candidate_limit == 2
    assert read_publication(path=path, before_as_of=current.as_of).selection_id == "late-backfill"
    first = read_publication(path=path, selection_id="z-first")
    store.publish("z-first", first.selection)
    assert read_publication(path=path, selection_id="z-first").published_at == first.published_at
    assert read_publication(path=path).selection_id == "a-second"
    assert read_publication(path=path, selection_id="missing") is None


def test_reader_separates_json_decode_from_schema_validation_without_changing_bytes(
    tmp_path, monkeypatch
):
    import baibai_engine.machine_analysis.screening.selection_query as query

    path = tmp_path / "runs"
    source = generation()
    publish_generation(source, path=path)
    selected = replace(
        empty_selection(source.generation_id),
        price_inputs={"flag": True, "integer": 0, "decimal": 0.0, "missing": None, "text": "値"},
    )
    store = ScreeningSelectionStore(path)
    store.publish("fixed", selected)
    expected = canonical_json(TypeAdapter(ScreeningSelection).dump_python(selected, mode="json"))
    with sqlite3.connect(path) as connection:
        blob = connection.execute("SELECT payload_gzip FROM screening_selection").fetchone()[0]
    assert gzip.decompress(blob) == expected.encode()
    before = path.read_bytes()

    def forbidden(*_args, **_kwargs):
        pytest.fail("full candidate result must not use the high-peak JSON validation path")

    monkeypatch.setattr(query._CODEC, "validate_json", forbidden)
    assert store.read("fixed") == selected
    assert query.read_publication(path=path, selection_id="fixed").selection == selected
    assert path.read_bytes() == before


def test_decoded_selection_still_rejects_invalid_policy(tmp_path):
    from baibai_engine.machine_analysis.screening.selection_query import read_publication

    path = tmp_path / "runs"
    source = generation()
    publish_generation(source, path=path)
    store = ScreeningSelectionStore(path)
    store.publish("fixed", empty_selection(source.generation_id))
    with sqlite3.connect(path) as connection:
        blob = connection.execute("SELECT payload_gzip FROM screening_selection").fetchone()[0]
        raw = json.loads(gzip.decompress(blob))
        raw["result"]["policy"]["method"] = "unknown-policy"
        connection.execute(
            "UPDATE screening_selection SET payload_gzip=?",
            (gzip.compress(canonical_json(raw).encode(), mtime=0),),
        )
    with pytest.raises(ValidationError):
        store.read("fixed")
    with pytest.raises(ValidationError):
        read_publication(path=path, selection_id="fixed")


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        (b"{", json.JSONDecodeError),
        (b"null", ValidationError),
        (b"[]", ValidationError),
        (b'{"generation_id":null}', ValidationError),
        ("null".encode("utf-16"), UnicodeDecodeError),
        ("null".encode("utf-32"), UnicodeDecodeError),
    ],
)
def test_decoded_selection_fails_closed_on_malformed_saved_json(tmp_path, payload, error):
    from baibai_engine.machine_analysis.screening.selection_query import read_publication

    path = tmp_path / "runs"
    source = generation()
    publish_generation(source, path=path)
    store = ScreeningSelectionStore(path)
    store.publish("fixed", empty_selection(source.generation_id))
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE screening_selection SET payload_gzip=?", (gzip.compress(payload, mtime=0),)
        )
    with pytest.raises(error):
        store.read("fixed")
    with pytest.raises(error):
        read_publication(path=path, selection_id="fixed")
