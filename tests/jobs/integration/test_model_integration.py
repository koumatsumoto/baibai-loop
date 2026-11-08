import hashlib
import json
from contextlib import contextmanager
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import TypeAdapter
from tests.helpers.edinet_native import empty_edinet_input
from tests.helpers.financial_native import seed_empty_financial
from tests.helpers.lake_release import market_store
from tests.helpers.r2_store import MemoryR2Store

import baibai_jobs.integration.models as job
from baibai_engine.data_collection.receipts.native_sources.store import initialize_database
from baibai_engine.machine_analysis.generation.contracts import (
    GeneratedModel,
    GenerationMetadata,
    GenerationSubject,
    InputSnapshot,
    ModelGenerationStream,
)
from baibai_engine.machine_analysis.generation.query import read_generation
from baibai_engine.machine_analysis.models.company_financials.earnings import EarningsPrice
from baibai_engine.machine_analysis.screening.policies.economic_paths import EconomicPathPolicy
from baibai_engine.machine_analysis.screening.selection_query import read_publication
from baibai_jobs.observability.discord import render_generation


@pytest.fixture
def integration(tmp_path, monkeypatch):
    events = []
    economic = tmp_path / "economic"
    initialize_database(economic).close()
    prepared = SimpleNamespace(
        release=SimpleNamespace(release_id="new-release"),
        serving=SimpleNamespace(pointer=None),
        release_sha256="a" * 64,
        source_snapshot=SimpleNamespace(sealed_at=datetime(2026, 10, 25, tzinfo=UTC)),
        price_identity=None,
        master_identity=None,
        margin_alerts_identity=None,
        margin_interest_identity=None,
        short_sale_reports_identity=None,
        calendar_identity=InputSnapshot(
            kind="native_l1",
            reference="calendar-native-fixed",
            sha256=hashlib.sha256(b"{}").hexdigest(),
            descriptor={},
        ),
    )

    @contextmanager
    def prepare(**kwargs):
        events.append("prepare")
        yield prepared
        events.append("release-snapshot")

    retain = job.retain_economic_input
    rebuild = job.rebuild_economic_input

    def retain_input(*args, **kwargs):
        events.append("retain")
        return retain(*args, **kwargs)

    def rebuild_input(*args, **kwargs):
        events.append("rebuild")
        return rebuild(*args, **kwargs)

    def generate(**kwargs):
        events.append("generate")
        assert kwargs["growth_start"] == arguments["growth_start"]
        assert kwargs["market_snapshot"] is prepared.source_snapshot
        assert kwargs["evaluation_time"] >= prepared.source_snapshot.sealed_at
        assert kwargs["price_identity"] is prepared.price_identity
        assert kwargs["master_identity"] is prepared.master_identity
        assert kwargs["calendar_identity"] is prepared.calendar_identity
        assert kwargs["margin_alerts_identity"] is prepared.margin_alerts_identity
        assert kwargs["margin_interest_identity"] is prepared.margin_interest_identity
        assert kwargs["short_sale_reports_identity"] is prepared.short_sale_reports_identity
        assert kwargs["market_release"].release_id == "new-release"
        fixed = kwargs["economic_input"]
        assert fixed.identity.kind == "native_l1"
        assert fixed.as_of == kwargs["as_of"]
        assert fixed.evaluation_time == kwargs["evaluation_time"]
        assert set(fixed.context) == {"reading", "tradingview_market"}
        return ModelGenerationStream(
            metadata=GenerationMetadata(
                schema_version=2,
                generation_id=kwargs["generation_id"],
                as_of=kwargs["as_of"],
                evaluation_time=datetime(2026, 10, 25, tzinfo=UTC),
                inputs={
                    "market": InputSnapshot(
                        kind="lake_release", reference="new-release", sha256="a" * 64
                    ),
                    "economic": fixed.identity,
                    "calendar": kwargs["calendar_identity"],
                    **(
                        {"edinet": kwargs["edinet_input"].identity}
                        if kwargs["edinet_input"]
                        else {}
                    ),
                },
                subject_plan={
                    "1234": GenerationSubject(
                        scope="stock", model_ids=("company_financials", "market_context")
                    ),
                    "5678": GenerationSubject(scope="stock", model_ids=("company_financials",)),
                    "market:global": GenerationSubject(
                        scope="market", model_ids=("social_trends",)
                    ),
                },
            ),
            subjects=("1234", "5678"),
            models=iter(
                (
                    GeneratedModel(
                        model_id="company_financials",
                        subject="1234",
                        calculation_revision="v1",
                        payload={
                            "expected_price_date": "2026-10-23",
                            "earnings_price": TypeAdapter(EarningsPrice).dump_python(
                                EarningsPrice(
                                    as_of=date(2026, 10, 23),
                                    earnings_yield=0.1,
                                    price_date=date(2026, 10, 23),
                                    price_yen=100.0,
                                    shares_ex_treasury=100.0,
                                    market_cap_yen=10000.0,
                                    profit_ttm_yen=1000.0,
                                    statement_basis="consolidated",
                                    profit_components=(),
                                    unavailable_reason=None,
                                ),
                                mode="json",
                            ),
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
        assert read_publication(path=tmp_path / "runs").selection_id == (
            "selection-models-new-release-best-economic-path-v4"
        )
        before_pointer_switch()
        events.append("publish")
        return SimpleNamespace(as_dict=lambda: {"release_id": "new-release"})

    monkeypatch.setattr(job, "_load_handle", lambda path: object())
    monkeypatch.setattr(job, "verify_held", lambda store, handle: events.append("lease"))
    monkeypatch.setattr(job, "prepare_market_lake", prepare)
    monkeypatch.setattr(job, "retain_economic_input", retain_input)
    monkeypatch.setattr(job, "rebuild_economic_input", rebuild_input)
    monkeypatch.setattr(job, "read_and_generate_models", generate)
    monkeypatch.setattr(job, "publish_prepared_market_lake", publish)
    store = MemoryR2Store()
    seed_empty_financial(store, tmp_path / "financial-native", date(2026, 10, 23))
    arguments = dict(
        store=store,
        edinet_input=empty_edinet_input(store, tmp_path),
        market=market_store(tmp_path / "market"),
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
    assert read_publication(path=arguments["runs"]).selection_id == result["selection_id"]
    assert publication.selection.result.policy.method == "best-economic-path-v4"
    assert result["selection_method"] == "best-economic-path-v4"
    assert (
        not {"screening_status", "market_updated", "delta_measured", "entered", "exited"}
        & result.keys()
    )
    assert result["candidate_count"] == 1
    assert result["company_count"] == 2
    assert result["model_count"] == 2
    assert render_generation(result) == [
        "モデル生成: 2社 / 2件 / 候補1社",
        "候補抽出の価格入力: 適格1/2社",
    ]
    assert result["screening_price_inputs"]["valid_count"] == 1
    assert result["screening_price_inputs"]["universe_count"] == 2
    assert result["growth_start"] == (None if growth_start is None else growth_start.isoformat())
    assert events == [
        "lease",
        "prepare",
        "retain",
        "rebuild",
        "generate",
        "lease",
        "publish",
        "release-snapshot",
    ]
    stored = read_generation(result["generation_id"], path=arguments["runs"])
    assert stored.inputs["market"].reference == result["lake"]["release_id"]
    assert stored.inputs["economic"].sha256 == result["economic_input_sha256"]
    assert stored.inputs["calendar"].reference == "calendar-native-fixed"


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
    monkeypatch.setattr(job, "evaluate_economic_coordinates", forbidden)
    monkeypatch.setattr(job, "prune_model_cache", forbidden)
    with pytest.raises(ValueError, match="late calculation failure"):
        job.integrate_models(**arguments)
    assert "publish" not in events
    assert read_generation("models-new-release", path=arguments["runs"]) is None


def test_models_retain_applied_edinet_input_without_current_heads(
    integration, tmp_path, monkeypatch
):
    from tests.jobs.integration.test_edinet_metric_rebuild import AT, sources

    from baibai_engine.data_collection.providers.edinet.archive_native import HEAD_KEY as ARCHIVES
    from baibai_engine.data_collection.providers.edinet.list_native import HEAD_KEY as LISTS
    from baibai_engine.machine_analysis.generation.edinet_inputs import retain_edinet_input

    arguments, _ = integration
    store, _, _, lists, archives = sources(tmp_path / "edinet")
    saved = retain_edinet_input(store, lists, archives, selected_at=AT)
    seed_empty_financial(store, tmp_path / "edinet-financial-native", date(2026, 10, 23))
    del store.values[LISTS]
    del store.values[ARCHIVES]
    store.get_keys.clear()
    prepare = Mock(wraps=job.prepare_market_lake)
    monkeypatch.setattr(job, "prepare_market_lake", prepare)
    result = job.integrate_models(**{**arguments, "store": store, "edinet_input": saved})
    assert prepare.call_args.kwargs["edinet_input"] == saved
    generated = read_generation(result["generation_id"], path=arguments["runs"])
    assert generated.inputs["edinet"] == saved
    assert LISTS not in store.get_keys
    assert ARCHIVES not in store.get_keys
    assert not any(key.endswith((".zip", ".body.gz")) for key in store.get_keys)


def test_native_models_require_the_applied_pair_before_preparing_any_release(
    integration, monkeypatch
):
    arguments, events = integration
    monkeypatch.setenv("BAIBAI_EDINET_LIST_NATIVE", "false")
    with pytest.raises(ValueError, match="requires its fixed EDINET input"):
        job.integrate_models(**{**arguments, "edinet_input": None})
    assert "prepare" not in events
    assert "generate" not in events
    assert "publish" not in events


@pytest.mark.parametrize("same_date", [True, False])
def test_cli_passes_the_applied_edinet_input_only_for_its_date(tmp_path, monkeypatch, same_date):
    from tests.jobs.integration.test_edinet_metric_rebuild import AT, sources

    from baibai_engine.machine_analysis.generation.edinet_inputs import retain_edinet_input

    store, _, _, lists, archives = sources(tmp_path / "edinet")
    saved = retain_edinet_input(store, lists, archives, selected_at=AT)
    report = tmp_path / "applied.json"
    report.write_text(
        json.dumps({"asof": "2026-10-23", "edinet_input": saved.model_dump(mode="json")})
    )
    seen = []
    monkeypatch.setattr(job, "Boto3R2Store", lambda **_: store)
    monkeypatch.setattr(
        job,
        "integrate_models",
        lambda **kwargs: seen.append(kwargs["edinet_input"]) or {"models_ready": True},
    )
    result = job.main(
        [
            "--asof",
            "2026-10-23" if same_date else "2026-10-24",
            "--lease-handle",
            str(tmp_path / "lease"),
            "--notice-output",
            str(tmp_path / "notice.json"),
            "--integration-report",
            str(report),
        ]
    )
    assert result == (0 if same_date else 1)
    assert seen == ([saved] if same_date else [])


@pytest.mark.parametrize("has_pointer", [False, True])
def test_comparison_evidence_uses_only_previous_serving_pointer(
    integration, monkeypatch, has_pointer
):
    arguments, _ = integration
    prepare = job.prepare_market_lake
    generate = Mock(wraps=job.read_and_generate_models)
    monkeypatch.setattr(job, "read_and_generate_models", generate)
    pointer = SimpleNamespace(release_id="canonical-serving", manifest_sha256="b" * 64)

    @contextmanager
    def with_serving(**kwargs):
        with prepare(**kwargs) as prepared:
            prepared.serving.pointer = pointer if has_pointer else None
            yield prepared

    monkeypatch.setattr(job, "prepare_market_lake", with_serving)
    native = InputSnapshot(
        kind="native_l1",
        reference="edinet-fixed",
        descriptor={},
        sha256=hashlib.sha256(b"{}").hexdigest(),
    )
    monkeypatch.setattr(job, "read_edinet_input", lambda *_: SimpleNamespace(identity=native))
    job.integrate_models(**{**arguments, "edinet_input": native})
    source = generate.call_args.kwargs["comparison_evidence"]
    if has_pointer:
        assert source.market == InputSnapshot(
            kind="lake_release", reference="canonical-serving", sha256="b" * 64
        )
        assert source.runs == arguments["runs"]
        assert source.market.reference != generate.call_args.kwargs["market_release"].release_id
    else:
        assert source is None
