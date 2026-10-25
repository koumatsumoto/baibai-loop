from contextlib import contextmanager
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from baibai_engine.machine_analysis.generation_store import ModelGenerationStore
from baibai_engine.machine_analysis.model_generation import (
    GeneratedModel,
    InputSnapshot,
    ModelGeneration,
)
from baibai_engine.machine_analysis.screening.selection_query import read_publication
from baibai_engine.machine_analysis.screening.views import TwoViewPolicy
from baibai_jobs.jobs import model_integration as job


@pytest.fixture
def integration(tmp_path, monkeypatch):
    events = []
    economic = tmp_path / "economic"
    economic.write_bytes(b"fixed-economic")
    prepared = SimpleNamespace(
        release=SimpleNamespace(release_id="new-release"),
        release_sha256="a" * 64,
        source_snapshot=object(),
    )

    @contextmanager
    def prepare(**kwargs):
        events.append("prepare")
        yield prepared
        events.append("release-snapshot")

    def snapshot(source, output):
        output.write_bytes(source.read_bytes())
        source.write_bytes(b"later-economic")

    def generate(**kwargs):
        events.append("generate")
        assert kwargs["market_snapshot"] is prepared.source_snapshot
        assert kwargs["market_release"].release_id == "new-release"
        assert kwargs["economic_path"].read_bytes() == b"fixed-economic"
        return ModelGeneration(
            generation_id=kwargs["generation_id"],
            as_of=kwargs["as_of"],
            evaluation_time=datetime(2026, 10, 25, tzinfo=UTC),
            inputs={
                "market": InputSnapshot(
                    kind="lake_release", reference="new-release", sha256="a" * 64
                ),
                "economic": kwargs["economic_identity"],
            },
            models=(
                GeneratedModel(
                    model_id="company_valuation",
                    subject="1234",
                    calculation_revision="v1",
                    payload={"earnings_price": {"earnings_yield": 0.1}},
                ),
                GeneratedModel(
                    model_id="technical_analysis",
                    subject="1234",
                    calculation_revision="v1",
                    payload={"price_momentum": {"value": None}},
                ),
            ),
        )

    def publish(received, *, store, before_pointer_switch):
        assert received is prepared
        assert read_publication(path=tmp_path / "runs").selection_id == "selection-new-release"
        before_pointer_switch()
        events.append("publish")
        return SimpleNamespace(as_dict=lambda: {"release_id": "new-release"})

    monkeypatch.setattr(job, "_load_handle", lambda path: object())
    monkeypatch.setattr(job, "verify_held", lambda store, handle: events.append("lease"))
    monkeypatch.setattr(job, "prepare_market_lake", prepare)
    monkeypatch.setattr(job, "create_snapshot", snapshot)
    monkeypatch.setattr(job, "read_and_generate_models", generate)
    monkeypatch.setattr(job, "publish_prepared_market_lake", publish)
    arguments = dict(
        store=object(),
        market=tmp_path / "market",
        economic=economic,
        runs=tmp_path / "runs",
        mirror=tmp_path / "mirror",
        as_of=date(2026, 10, 23),
        lease_handle=tmp_path / "lease",
        growth_start=date(2023, 10, 23),
        policy=TwoViewPolicy(earnings_limit=20, combined_limit=20),
    )
    return arguments, events


def test_models_policy_and_l1_share_the_same_fixed_inputs_without_web(integration):
    arguments, events = integration
    result = job.integrate_models(**arguments)
    assert result["models_ready"] is True
    assert result["candidate_count"] == 1
    assert events == ["lease", "prepare", "generate", "lease", "publish", "release-snapshot"]
    stored = ModelGenerationStore(arguments["runs"]).read(result["generation_id"])
    assert stored.inputs["market"].reference == result["lake"]["release_id"]
    assert stored.inputs["economic"].sha256 == result["economic_snapshot_sha256"]


def test_generation_failure_does_not_publish_or_save_a_partial_run(integration, monkeypatch):
    arguments, events = integration

    def fail(**kwargs):
        raise ValueError("invalid model input")

    monkeypatch.setattr(job, "read_and_generate_models", fail)
    with pytest.raises(ValueError, match="invalid model input"):
        job.integrate_models(**arguments)
    assert "publish" not in events
    assert not arguments["runs"].exists()


def test_failed_lake_publication_does_not_prune_existing_generations(integration, monkeypatch):
    arguments, events = integration

    def fail(*args, **kwargs):
        raise ValueError("publication failed")

    def forbidden(*args, **kwargs):
        raise AssertionError("must not prune before publication succeeds")

    monkeypatch.setattr(job, "publish_prepared_market_lake", fail)
    monkeypatch.setattr(ModelGenerationStore, "prune", forbidden)
    with pytest.raises(ValueError, match="publication failed"):
        job.integrate_models(**arguments)
    assert "publish" not in events
