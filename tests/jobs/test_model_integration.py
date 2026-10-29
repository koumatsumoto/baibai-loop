from contextlib import contextmanager
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

import baibai_jobs.integration.models as job
from baibai_engine.machine_analysis.generation.contracts import (
    GeneratedModel,
    GenerationMetadata,
    InputSnapshot,
    ModelGenerationStream,
)
from baibai_engine.machine_analysis.generation.store import ModelGenerationStore
from baibai_engine.machine_analysis.screening.policies.economic_paths import EconomicPathPolicy
from baibai_engine.machine_analysis.screening.selection_query import read_publication
from baibai_jobs.observability.discord import render_generation


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
        assert kwargs["growth_start"] == arguments["growth_start"]
        assert kwargs["market_snapshot"] is prepared.source_snapshot
        assert kwargs["market_release"].release_id == "new-release"
        assert kwargs["economic_path"].read_bytes() == b"fixed-economic"
        return ModelGenerationStream(
            metadata=GenerationMetadata(
                generation_id=kwargs["generation_id"],
                as_of=kwargs["as_of"],
                evaluation_time=datetime(2026, 10, 25, tzinfo=UTC),
                inputs={
                    "market": InputSnapshot(
                        kind="lake_release", reference="new-release", sha256="a" * 64
                    ),
                    "economic": kwargs["economic_identity"],
                },
            ),
            subjects=("1234", "5678"),
            models=iter(
                (
                    GeneratedModel(
                        model_id="company_valuation",
                        subject="1234",
                        calculation_revision="v1",
                        payload={
                            "expected_price_date": "2026-10-23",
                            "earnings_price": {
                                "earnings_yield": 0.1,
                                "price_date": "2026-10-23",
                                "price_yen": 100.0,
                                "shares_ex_treasury": 100.0,
                                "market_cap_yen": 10000.0,
                            },
                            "financial": {"pbr": 0.5, "market_cap": 10000.0},
                        },
                    ),
                    GeneratedModel(
                        model_id="market_context",
                        subject="1234",
                        calculation_revision="v1",
                        payload={"master": {"name": "合成企業", "sector_33": "情報・通信業"}},
                    ),
                )
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
        policy=EconomicPathPolicy(candidate_limit=36),
    )
    return arguments, events


@pytest.mark.parametrize("growth_start", [None, date(2023, 10, 23)])
def test_models_policy_and_lake_share_the_same_fixed_inputs_without_web(integration, growth_start):
    arguments, events = integration
    arguments["growth_start"] = growth_start
    result = job.integrate_models(**arguments)
    assert result["models_ready"] is True
    publication = read_publication(path=arguments["runs"])
    assert publication.selection.result.policy.method == "best-economic-path-v3"
    assert (
        not {"screening_status", "market_updated", "delta_measured", "entered", "exited"}
        & result.keys()
    )
    assert result["candidate_count"] == 1
    assert result["company_count"] == 2
    assert result["model_count"] == 2
    assert render_generation(result) == ["モデル生成: 2社 / 2件 / 候補1社"]
    assert result["growth_start"] == (None if growth_start is None else growth_start.isoformat())
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
    monkeypatch.setattr(job, "prune_model_cache", forbidden)
    with pytest.raises(ValueError, match="publication failed"):
        job.integrate_models(**arguments)
    assert "publish" not in events


@pytest.mark.parametrize("start", [None, "2020-01-01"])
def test_cli_does_not_implicitly_truncate_growth_history(tmp_path, monkeypatch, start):
    received = []
    monkeypatch.setattr(job, "Boto3R2Store", lambda **kwargs: object())

    def integrate(**kwargs):
        received.append(kwargs["growth_start"])
        return {"models_ready": True}

    monkeypatch.setattr(job, "integrate_models", integrate)
    arguments = [
        "--asof",
        "2026-10-23",
        "--lease-handle",
        str(tmp_path / "lease"),
        "--notice-output",
        str(tmp_path / "notice.json"),
    ]
    if start is not None:
        arguments += ["--growth-start", start]
    assert job.main(arguments) == 0
    assert received == [None if start is None else date.fromisoformat(start)]


def test_lazy_generation_failure_stops_selection_lake_and_pruning(integration, monkeypatch):
    arguments, events = integration
    generate = job.read_and_generate_models

    def late_failure(**kwargs):
        original = generate(**kwargs)

        def rows():
            yield next(original.models)
            raise ValueError("late calculation failure")

        return ModelGenerationStream(original.metadata, original.subjects, rows())

    def forbidden(*args, **kwargs):
        raise AssertionError("must not continue after failed generation")

    monkeypatch.setattr(job, "read_and_generate_models", late_failure)
    monkeypatch.setattr(job, "select_saved_economic_generation", forbidden)
    monkeypatch.setattr(job, "prune_model_cache", forbidden)
    with pytest.raises(ValueError, match="late calculation failure"):
        job.integrate_models(**arguments)
    assert "publish" not in events
    assert ModelGenerationStore(arguments["runs"]).read("models-new-release") is None
