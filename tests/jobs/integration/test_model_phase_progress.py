"""処理の開始をflushし、成功境界だけに完了を出す。私有値は表示しない。"""

import io
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from tests.helpers.lake_release import market_store
from tests.jobs.integration.test_model_integration import integration as integration
from tests.jobs.publication import test_calendar_native_objects as calendar
from tests.jobs.publication.test_l1_price_export import price_source

from baibai_engine.machine_analysis.generation import inputs
from baibai_engine.machine_analysis.generation.contracts import ModelGenerationStream
from baibai_engine.machine_analysis.generation.store import ModelGenerationStore
from baibai_engine.machine_analysis.models.company_financials.metrics.models import (
    DEFAULT_TTM_RULES,
)
from baibai_engine.machine_analysis.models.company_growth.models import InputRelease
from baibai_engine.storage.snapshots import writer
from baibai_jobs.integration import models
from baibai_jobs.publication import snapshot_publication as publication
from baibai_jobs.publication.datasets import LAKE_DATASETS
from baibai_jobs.publication.l1_price_export import export_prices_from_l1

CANARY = "private-path-request-release-or-error"


class FlushedLog(io.StringIO):
    flushed = ""

    def flush(self):
        self.flushed = self.getvalue()


@pytest.fixture
def progress(monkeypatch):
    output = FlushedLog()
    for module in (writer, publication, models, inputs):
        monkeypatch.setattr(module, "sys", SimpleNamespace(stderr=output))
    return output


def assert_started(progress, phase):
    assert f"phase={phase} state=start\n" in progress.flushed
    assert f"phase={phase} state=done" not in progress.flushed


@pytest.mark.parametrize("module", [writer, publication, models, inputs])
def test_phase_messages_flush_only_fixed_label_and_elapsed(module, monkeypatch, progress):
    ticks = iter((10.0, 12.125))
    monkeypatch.setattr(module, "time", SimpleNamespace(perf_counter=lambda: next(ticks)))
    started = module._phase_progress("fixed")
    assert_started(progress, "fixed")
    module._phase_progress("fixed", started)
    assert progress.flushed.endswith("phase=fixed state=done elapsed_seconds=2.125\n")
    assert len(progress.flushed.splitlines()) == 2


@pytest.mark.parametrize("failure", [False, True])
def test_seal_done_means_ready_not_context_closed(tmp_path, monkeypatch, progress, failure):
    market = market_store(tmp_path / CANARY / "market.sqlite")
    backup = writer.backup_to_new_file
    digest = writer.sha256_file

    def observed_backup(*args, **kwargs):
        assert_started(progress, "seal")
        return backup(*args, **kwargs)

    def observed_digest(*args, **kwargs):
        assert_started(progress, "seal")
        if failure:
            raise ValueError(CANARY)
        return digest(*args, **kwargs)

    monkeypatch.setattr(writer, "backup_to_new_file", observed_backup)
    monkeypatch.setattr(writer, "sha256_file", observed_digest)
    if failure:
        with (
            pytest.raises(writer.LakeBuildError, match=CANARY),
            writer.sealed_sqlite_snapshot(sqlite_path=market, mirror_root=tmp_path / "mirror"),
        ):
            pytest.fail("failed seal must not be yielded")
        assert_started(progress, "seal")
    else:
        with writer.sealed_sqlite_snapshot(
            sqlite_path=market, mirror_root=tmp_path / "mirror"
        ) as sealed:
            assert sealed.path.is_file()
            assert "phase=seal state=done elapsed_seconds=" in progress.flushed
    assert CANARY not in progress.getvalue()


@pytest.mark.parametrize("failure", [False, True])
def test_dataset_done_follows_full_parity(tmp_path, monkeypatch, progress, failure):
    market = market_store(tmp_path / CANARY / "market.sqlite")
    dataset = LAKE_DATASETS["jquants.daily_bars"]
    parity = writer.validate_snapshot_parity

    def observed_parity(**kwargs):
        assert_started(progress, "export." + dataset.name)
        if failure:
            raise ValueError(CANARY)
        return parity(**kwargs)

    monkeypatch.setattr(writer, "validate_snapshot_parity", observed_parity)
    with writer.sealed_sqlite_snapshot(
        sqlite_path=market, mirror_root=tmp_path / "mirror"
    ) as sealed:
        arguments = dict(
            dataset=dataset,
            source_snapshot=sealed,
            mirror_root=tmp_path / "mirror",
            producer_git_commit="a" * 40,
            build_id=CANARY,
        )
        if failure:
            with pytest.raises(ValueError, match=CANARY):
                writer.export_dataset_snapshot(**arguments)
            assert_started(progress, "export." + dataset.name)
        else:
            report = writer.export_dataset_snapshot(**arguments)
            assert report.manifest_path.is_file()
            assert f"phase=export.{dataset.name} state=done elapsed_seconds=" in progress.flushed
    assert CANARY not in progress.getvalue()


def test_independent_l1_export_reports_its_dataset(tmp_path, progress):
    mirror, source = price_source(
        tmp_path, {"data": [{"Code": "13010", "Date": "2026-02-01", "C": 110}]}
    )
    report = export_prices_from_l1(
        source=source,
        mirror_root=mirror,
        producer_git_commit="a" * 40,
        known_at=datetime(2026, 2, 4, tzinfo=UTC),
        build_id=CANARY,
    )
    assert report.build.manifest.totals.rows == 2
    assert "phase=export.jquants.daily_bars state=start\n" in progress.flushed
    assert "phase=export.jquants.daily_bars state=done elapsed_seconds=" in progress.flushed
    assert CANARY not in progress.getvalue()


def test_native_projection_and_prepare_boundaries(tmp_path, monkeypatch, progress):
    calendar.configure(monkeypatch)
    store, source = calendar.owner(tmp_path / "calendar")
    source.commit([calendar.attempt(CANARY)])
    calendar.native_dependencies(store, tmp_path)
    for phase in ("calendar", "master", "price"):
        name = f"native_{phase}_model_input"
        original = getattr(publication, name)

        def observed(*args, _phase=phase, _original=original, **kwargs):
            assert_started(progress, "native_" + _phase)
            return _original(*args, **kwargs)

        monkeypatch.setattr(publication, name, observed)
    market = market_store(tmp_path / CANARY / "market.sqlite")
    with publication.prepare_market_lake(
        sqlite_path=market,
        mirror_root=tmp_path / "mirror",
        store=store,
        as_of=calendar.DAY,
        release_id=CANARY,
    ) as prepared:
        assert prepared.release.release_id == CANARY
        for phase in (
            "native_calendar",
            "native_master",
            "native_price",
            "base_resolve",
            "release_prepare",
        ):
            assert f"phase={phase} state=done elapsed_seconds=" in progress.flushed
    assert CANARY not in progress.getvalue()
    assert "lake/pointers/market/current.json" not in store.values


@pytest.mark.parametrize("failure", [False, True])
def test_model_generation_done_waits_for_lazy_stream(integration, monkeypatch, progress, failure):
    arguments, events = integration
    generate = models.read_and_generate_models

    def observed_generate(**kwargs):
        assert_started(progress, "model_inputs")
        original = generate(**kwargs)

        def rows():
            assert "phase=model_inputs state=done" in progress.flushed
            assert_started(progress, "model_generation")
            yield next(original.models)
            if failure:
                raise ValueError(CANARY)
            yield from original.models

        return ModelGenerationStream(original.metadata, original.subjects, rows())

    monkeypatch.setattr(models, "read_and_generate_models", observed_generate)
    if failure:
        with pytest.raises(ValueError, match=CANARY):
            models.integrate_models(**arguments)
        assert_started(progress, "model_generation")
        assert "phase=screening" not in progress.flushed
        assert "phase=lake_publish" not in progress.flushed
        assert "publish" not in events
        assert ModelGenerationStore(arguments["runs"]).read("models-new-release") is None
    else:
        result = models.integrate_models(**arguments)
        assert result["models_ready"] is True
        phases = (
            "economic_input",
            "model_inputs",
            "model_generation",
            "screening",
            "lake_publish",
            "model_cache_prune",
        )
        expected = [
            f"phase={phase} state={state}" for phase in phases for state in ("start", "done")
        ]
        lines = [
            "phase=" + line.split("phase=", 1)[1].split(" elapsed_seconds=")[0]
            for line in progress.flushed.splitlines()
        ]
        assert lines == expected
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
    assert CANARY not in progress.getvalue()


@pytest.mark.parametrize(
    ("phase", "operation"),
    [
        ("economic_input", "retain_economic_input"),
        ("model_inputs", "read_and_generate_models"),
        ("screening", "evaluate_saved_economic_generation"),
        ("lake_publish", "publish_prepared_market_lake"),
    ],
)
def test_failed_phase_never_logs_done_or_prunes(
    integration, monkeypatch, progress, phase, operation
):
    arguments, events = integration
    error = ValueError(CANARY)

    def fail(*args, **kwargs):
        assert_started(progress, phase)
        raise error

    monkeypatch.setattr(models, operation, fail)
    with pytest.raises(ValueError, match=CANARY) as caught:
        models.integrate_models(**arguments)
    assert caught.value is error
    assert_started(progress, phase)
    assert "phase=model_cache_prune" not in progress.flushed
    assert "publish" not in events
    assert CANARY not in progress.getvalue()


def test_native_financial_failure_keeps_start_only(integration, monkeypatch, progress):
    arguments, _events = integration
    monkeypatch.setenv("BAIBAI_FINANCIAL_NATIVE", "true")
    monkeypatch.setattr(
        models, "FinancialL1", lambda *args: SimpleNamespace(fixed=lambda: object())
    )
    error = ValueError(CANARY)

    def fail(*args, **kwargs):
        assert_started(progress, "native_financial")
        raise error

    monkeypatch.setattr(models, "native_financial_model_input", fail)
    with pytest.raises(ValueError, match=CANARY) as caught:
        models.integrate_models(**arguments)
    assert caught.value is error
    assert_started(progress, "native_financial")
    assert "phase=economic_input" not in progress.flushed
    assert CANARY not in progress.getvalue()


@pytest.mark.parametrize(
    "failure", [None, "edinet_comparisons", "pdf_financials", "holder_capital", "price_series"]
)
def test_input_read_boundaries_preserve_failure_and_holder_fallback(
    tmp_path, monkeypatch, progress, caplog, failure
):
    phases = {
        "edinet_comparisons": "read_comparison_population",
        "pdf_financials": "read_pdf_statements",
        "holder_capital": "read_holder_capital",
        "price_series": "read_price_bars",
    }
    error = ValueError(CANARY)
    for phase, name in phases.items():
        original = getattr(inputs, name)

        def observed(*args, _phase=phase, _original=original, **kwargs):
            assert_started(progress, _phase)
            if failure == _phase:
                raise error
            return _original(*args, **kwargs)

        monkeypatch.setattr(inputs, name, observed)
    market = market_store(tmp_path / CANARY / "market.sqlite")
    with writer.sealed_sqlite_snapshot(
        sqlite_path=market, mirror_root=tmp_path / "mirror"
    ) as sealed:
        arguments = dict(
            generation_id=CANARY,
            market_snapshot=sealed,
            market_release=InputRelease(release_id=CANARY, manifest_sha256="a" * 64),
            economic_input=None,
            as_of=calendar.DAY,
            evaluation_time=calendar.AT,
            ttm_rules=DEFAULT_TTM_RULES,
            tickers=("1301",),
        )
        if failure in (None, "holder_capital"):
            generated = inputs.read_and_generate_models(**arguments)
            assert generated.subjects == ("1301",)
            for phase in phases:
                if phase != failure:
                    assert f"phase={phase} state=done elapsed_seconds=" in progress.flushed
        else:
            with pytest.raises(ValueError, match=CANARY) as caught:
                inputs.read_and_generate_models(**arguments)
            assert caught.value is error
        if failure is not None:
            assert_started(progress, failure)
    assert CANARY not in progress.getvalue()
    assert CANARY not in caplog.text
