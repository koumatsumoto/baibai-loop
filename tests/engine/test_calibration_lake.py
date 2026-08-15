from __future__ import annotations

import csv
import hashlib
import json
import threading
from dataclasses import fields as dc_fields
from datetime import UTC, date, datetime, timedelta
from os import link
from pathlib import Path
from shutil import copytree

import pytest
import yaml
from pydantic import ValidationError
from tests.helpers.calibration_store import (
    publish_forward,
    publish_panel,
)

from baibai_engine.market.lake.keys import (
    current_calibration_bundle_pointer_key,
    pin_key,
)
from baibai_engine.market.lake.models import (
    CalibrationDatasetRef,
    CalibrationInputFile,
    CalibrationInputManifest,
    CalibrationInputSourceRef,
    CohortInventoryEntry,
    DatasetManifest,
    RawIngestSourceRef,
    canonical_lake_model_bytes,
)
from baibai_engine.market.lake.retention import (
    LakeRetentionError,
    apply_gc,
    create_pin,
    lake_writer_lock,
    plan_gc,
    read_pins,
    remove_pin,
)
from baibai_engine.screening.calibration import lake as lake_module
from baibai_engine.screening.calibration import legacy_csv as legacy_csv_module
from baibai_engine.screening.calibration import store
from baibai_engine.screening.calibration.forward import (
    DEFAULT_FORWARD_OBSERVATION_POLICY,
    ForwardObservationPolicy,
    ForwardReturnRow,
)
from baibai_engine.screening.calibration.lake import (
    CALIBRATION_DIAGNOSTICS,
    CALIBRATION_FORWARD,
    CALIBRATION_PANEL,
    CalibrationBundlePointer,
    CalibrationLakeError,
    load_manifest,
    require_build_inputs,
    require_l2_dataset,
    transform_fingerprint,
)
from baibai_engine.screening.calibration.legacy_csv import (
    legacy_cohorts,
    migrate_legacy_calibration,
    read_legacy_forward,
    read_legacy_panel,
    read_legacy_panel_meta,
)
from baibai_engine.screening.calibration.panel import PanelDiagnostics, PanelRow
from baibai_engine.screening.calibration.store import (
    CACHE_SCHEMA_VERSION,
    CalibrationCacheError,
    adopt_bundle_generation,
    current_bundle_ref,
    has_cohort,
    published_cohorts,
    read_forward,
    read_panel,
    read_panel_meta,
    resolve_calibration_bundle,
)

_JANUARY = "2026-01-30"
_FEBRUARY = "2026-02-27"
_MARCH = "2026-03-31"
_APRIL = "2026-04-30"


def _cohort(asof: str, tickers: tuple[str, ...] = ("1301", "7203")) -> list[dict[str, object]]:
    return [
        {
            "ticker": ticker,
            "er_annual": 0.10 + index / 100,
            "market_cap_oku": 500.0,
            "avg_turnover_oku": 5.0,
            "listing_span_days": 900.0,
            "selection_rank": index + 1,
            "pass_screen": True,
        }
        for index, ticker in enumerate(tickers)
    ]


def _forward_rows(asof: str) -> list[dict[str, object]]:
    return [{"ticker": "1301", "horizon": "1y", "status": "unresolved_future_horizon"}]


def _manifest(root: Path, dataset_name: str) -> DatasetManifest:
    return store.resolve_calibration_bundle(root).datasets[dataset_name]


def _dataset_ref(root: Path, dataset_name: str) -> CalibrationDatasetRef:
    return store.resolve_calibration_bundle(root).manifest.datasets[dataset_name]


def _bundle_pointer(root: Path) -> CalibrationBundlePointer:
    return CalibrationBundlePointer.model_validate_json(
        (root / current_calibration_bundle_pointer_key()).read_bytes()
    )


class TestTypedContract:
    def test_the_arrow_schema_is_derived_from_the_row_contract(self) -> None:
        schema = CALIBRATION_PANEL.arrow_schema

        assert [field.name for field in schema] == list(CALIBRATION_PANEL.field_names)
        assert schema.metadata[b"baibai.dataset"] == CALIBRATION_PANEL.name.encode()
        assert (
            schema.metadata[b"baibai.contract_version"]
            == str(CALIBRATION_PANEL.contract_version).encode()
        )
        # A field the row declares as optional has to be storable as null; one it
        # declares as required must not be.
        by_name = {field.name: field for field in schema}
        assert by_name["er_annual"].nullable is True
        assert by_name["ticker"].nullable is False

    def test_a_dataset_outside_the_contract_is_refused(self) -> None:
        with pytest.raises(CalibrationLakeError, match="unsupported L2 calibration dataset"):
            require_l2_dataset("calibration.something_else")

    def test_the_transform_fingerprint_tracks_the_measurement_rules(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        same = transform_fingerprint(CALIBRATION_PANEL, cache_schema_version=CACHE_SCHEMA_VERSION)
        other = transform_fingerprint(CALIBRATION_PANEL, cache_schema_version="0" * 16)

        assert same == transform_fingerprint(
            CALIBRATION_PANEL, cache_schema_version=CACHE_SCHEMA_VERSION
        )
        assert same != other
        original = lake_module.sha256_file
        monkeypatch.setattr(
            lake_module,
            "sha256_file",
            lambda path: "0" * 64 if path.name == "panel.py" else original(path),
        )
        assert same != transform_fingerprint(
            CALIBRATION_PANEL, cache_schema_version=CACHE_SCHEMA_VERSION
        )


def _retained_calibration_source(root: Path) -> tuple[Path, CalibrationInputSourceRef]:
    """A cohort source whose bytes the lake keeps, and the archived file it names."""

    payload = b"retired calibration cache\n"
    digest = hashlib.sha256(payload).hexdigest()
    input_id = f"retired-{digest[:24]}"
    file_key = f"lake/l2/calibration-legacy/{input_id}/panel.csv"
    archived = root / file_key
    archived.parent.mkdir(parents=True, exist_ok=True)
    archived.write_bytes(payload)
    manifest = CalibrationInputManifest(
        manifest_version=1,
        input_id=input_id,
        input_type="legacy_csv_archive",
        files={"panel.csv": CalibrationInputFile(key=file_key, sha256=digest, bytes=len(payload))},
    )
    manifest_key = f"lake/manifests/calibration-inputs/{input_id}.json"
    manifest_payload = canonical_lake_model_bytes(manifest)
    manifest_path = root / manifest_key
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_bytes(manifest_payload)
    return archived, CalibrationInputSourceRef(
        kind="calibration_input",
        source_id=input_id,
        key=manifest_key,
        sha256=hashlib.sha256(manifest_payload).hexdigest(),
        input_type="legacy_csv_archive",
        manifest_version=1,
    )


def _raw_ingest_source() -> RawIngestSourceRef:
    digest = "b" * 64
    return RawIngestSourceRef(
        kind="raw_ingest",
        source_id="20260130T000000Z-ingest",
        key="lake/l1/raw/jquants/daily_bars/ingest_date=2026-01-30/20260130T000000Z-ingest.json.gz",
        sha256=digest,
        provider="jquants",
        dataset="daily_bars",
        request_start=date(2026, 1, 1),
        request_end=date(2026, 1, 30),
        metadata_version=1,
        metadata_key=(
            "lake/l1/raw/jquants/daily_bars/ingest_date=2026-01-30/"
            "20260130T000000Z-ingest.json.gz.metadata.json"
        ),
        metadata_sha256="c" * 64,
    )


class TestImmutableBuilds:
    def test_the_manifest_states_which_rules_each_cohort_was_measured_under(
        self, tmp_path: Path
    ) -> None:
        """A consumer must not have to open Parquet to learn which rules produced a row.

        The rules decide membership and status, so two cohorts under different rules are
        not one series. Leaving that only inside a diagnostics row means the manifest
        cannot say what the generation is, and a mixture is assemblable without anything
        in it disagreeing.
        """

        publish_panel(tmp_path, _JANUARY, _cohort(_JANUARY), rules_hash="rules-one")
        publish_forward(
            tmp_path,
            _JANUARY,
            [{"ticker": "1301", "horizon": "1y", "price_return": 0.2, "status": "resolved"}],
        )

        bundle = store.resolve_calibration_bundle(tmp_path)
        cohort = bundle.manifest.cohorts[_JANUARY]

        assert cohort.panel.measurement_policy.rules_hash == "rules-one"
        assert cohort.panel.measurement_policy.panel_variant == "production"
        assert cohort.panel.measurement_policy.production_authority is True
        # The outcome inherits the panel's identity: it observed the names that panel
        # selected, so claiming other rules would describe a cross-section it never used.
        assert cohort.forward.measurement_policy == cohort.panel.measurement_policy
        assert cohort.diagnostics.measurement_policy == cohort.panel.measurement_policy

    def test_a_forward_cohort_without_a_panel_has_no_rules_to_inherit(self, tmp_path: Path) -> None:
        with pytest.raises(CalibrationCacheError, match="no panel to inherit rules from"):
            publish_forward(
                tmp_path,
                _JANUARY,
                [{"ticker": "1301", "horizon": "1y", "price_return": 0.2, "status": "resolved"}],
            )

    def test_a_cohort_cannot_name_provider_raw_as_its_source(self, tmp_path: Path) -> None:
        """An analytical cohort is built from a fixed generation, never a request range.

        Raw is addressed by provider and date range and can be fetched again, so a
        cohort naming it would be pinned to nothing. The kind is outside the cohort
        source union, which makes the claim unrepresentable rather than merely refused.
        """

        del tmp_path
        with pytest.raises(ValidationError):
            CohortInventoryEntry(
                status="empty",
                rows=0,
                sources=(_raw_ingest_source(),),  # type: ignore[arg-type]
                input_cutoff=date.fromisoformat(_JANUARY),
            )

    def test_a_cohort_is_published_as_a_build_the_pointer_names(self, tmp_path: Path) -> None:
        publish_panel(tmp_path, _JANUARY, _cohort(_JANUARY))

        for dataset in (CALIBRATION_PANEL, CALIBRATION_DIAGNOSTICS):
            reference = _dataset_ref(tmp_path, dataset.name)
            manifest = load_manifest(tmp_path / reference.manifest_key)
            assert manifest.layer == "l2_analytical"
            assert manifest.build_id == reference.build_id
            assert manifest.sources == ()
            assert {
                source.kind
                for cohort in manifest.cohort_inventory.values()
                for source in cohort.sources
            } == {"sqlite_snapshot"}
        assert published_cohorts(tmp_path) == [date.fromisoformat(_JANUARY)]

    def test_a_second_cohort_reuses_the_first_month_object(self, tmp_path: Path) -> None:
        """A month that did not change must not be rewritten by the next cohort."""

        publish_panel(tmp_path, _JANUARY, _cohort(_JANUARY))
        january_keys = {
            item.key
            for partition in _manifest(tmp_path, CALIBRATION_PANEL.name).partitions
            for item in partition.objects
        }

        publish_panel(tmp_path, _FEBRUARY, _cohort(_FEBRUARY))
        manifest = _manifest(tmp_path, CALIBRATION_PANEL.name)
        keys = {item.key for partition in manifest.partitions for item in partition.objects}

        assert january_keys < keys
        assert len(manifest.partitions) == 2
        assert published_cohorts(tmp_path) == [
            date.fromisoformat(_JANUARY),
            date.fromisoformat(_FEBRUARY),
        ]

    def test_panel_and_forward_keep_their_own_input_generation(self, tmp_path: Path) -> None:
        publish_panel(
            tmp_path,
            _JANUARY,
            _cohort(_JANUARY),
            source_label="panel-snapshot",
        )
        publish_forward(
            tmp_path,
            _JANUARY,
            [{"ticker": "1301", "horizon": "1y", "status": "unresolved_future_horizon"}],
            source_label="forward-snapshot",
            input_cutoff=date(2027, 1, 31),
        )

        bundle = resolve_calibration_bundle(tmp_path).manifest.cohorts[_JANUARY]

        assert bundle.panel.sources == bundle.diagnostics.sources
        assert bundle.panel.sources != bundle.forward.sources
        assert bundle.panel.input_cutoff == date.fromisoformat(_JANUARY)
        assert bundle.forward.input_cutoff == date(2027, 1, 31)

    def test_manifest_reader_rejects_duplicate_fields_without_echoing_values(
        self, tmp_path: Path
    ) -> None:
        publish_panel(tmp_path, _JANUARY, _cohort(_JANUARY))
        path = tmp_path / _dataset_ref(tmp_path, CALIBRATION_PANEL.name).manifest_key
        secret = "credential-must-not-appear"
        raw = path.read_bytes().replace(
            b'"manifest_version":1',
            f'"manifest_version":1,"manifest_version":1,"unknown":"{secret}"'.encode(),
            1,
        )
        path.write_bytes(raw)

        with pytest.raises(CalibrationLakeError) as captured:
            load_manifest(path)

        assert secret not in str(captured.value)

    def test_manifest_install_failure_never_moves_the_bundle_pointer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        publish_panel(tmp_path, _JANUARY, _cohort(_JANUARY))
        before = _bundle_pointer(tmp_path).current

        def fail_install(_path: Path, _payload: bytes) -> None:
            raise OSError("injected durable install failure")

        monkeypatch.setattr(lake_module, "install_immutable_bytes", fail_install)
        with pytest.raises(CalibrationCacheError, match="durable install"):
            publish_panel(tmp_path, _FEBRUARY, _cohort(_FEBRUARY))

        assert _bundle_pointer(tmp_path).current == before
        assert read_panel(tmp_path, date.fromisoformat(_JANUARY))

    def test_rebuilding_one_cohort_leaves_the_other_month_untouched(self, tmp_path: Path) -> None:
        publish_panel(tmp_path, _JANUARY, _cohort(_JANUARY))
        publish_panel(tmp_path, _FEBRUARY, _cohort(_FEBRUARY))
        before = {
            (int(p.values["year"]), int(p.values["month"])): p.objects[0].key
            for p in _manifest(tmp_path, CALIBRATION_PANEL.name).partitions
        }

        publish_panel(tmp_path, _FEBRUARY, _cohort(_FEBRUARY, tickers=("1301",)))
        after = {
            (int(p.values["year"]), int(p.values["month"])): p.objects[0].key
            for p in _manifest(tmp_path, CALIBRATION_PANEL.name).partitions
        }

        assert before[(2026, 1)] == after[(2026, 1)]
        assert before[(2026, 2)] != after[(2026, 2)]
        assert [row.ticker for row in read_panel(tmp_path, date.fromisoformat(_FEBRUARY))] == [
            "1301"
        ]

    def test_the_previous_build_stays_addressable_after_the_generation_moves(
        self, tmp_path: Path
    ) -> None:
        publish_panel(tmp_path, _JANUARY, _cohort(_JANUARY))
        first = _dataset_ref(tmp_path, CALIBRATION_PANEL.name)

        publish_panel(tmp_path, _FEBRUARY, _cohort(_FEBRUARY))
        second = _dataset_ref(tmp_path, CALIBRATION_PANEL.name)

        assert second.build_id != first.build_id
        assert _bundle_pointer(tmp_path).previous is not None
        assert (tmp_path / first.manifest_key).is_file()

    def test_a_second_run_over_the_same_cohorts_matures_forward_only(self, tmp_path: Path) -> None:
        """The published bundle is the whole of what the next run continues from.

        A generation is built by hard-linking the store into a work directory, so any
        writer state the store does not publish is simply not there next time. Splitting
        the published pointer from the writer's continuation makes the second run of the
        same range — which is how forward outcomes mature — fail on state nobody kept.
        """

        source = tmp_path / "store"
        publish_panel(source, _JANUARY, _cohort(_JANUARY))
        publish_forward(source, _JANUARY, _forward_rows(_JANUARY))
        root = tmp_path / "adopted"
        adopt_bundle_generation(root, source, expected_current=None)
        first = store.resolve_calibration_bundle(root).manifest

        second_generation = tmp_path / "second"
        copytree(root, second_generation, copy_function=link)
        publish_forward(
            second_generation,
            _JANUARY,
            [{"ticker": "1301", "horizon": "1y", "price_return": 0.3, "status": "resolved"}],
        )
        adopt_bundle_generation(
            root, second_generation, expected_current=store.current_bundle_ref(root)
        )
        second = store.resolve_calibration_bundle(root).manifest

        assert second.datasets[CALIBRATION_PANEL.name] == first.datasets[CALIBRATION_PANEL.name]
        assert (
            second.datasets[CALIBRATION_DIAGNOSTICS.name]
            == first.datasets[CALIBRATION_DIAGNOSTICS.name]
        )
        assert second.datasets[CALIBRATION_FORWARD.name] != first.datasets[CALIBRATION_FORWARD.name]
        assert read_forward(root, date.fromisoformat(_JANUARY))[0].price_return == 0.3

    def test_a_store_holds_no_writer_state_outside_its_bundle_pointer(self, tmp_path: Path) -> None:
        source = tmp_path / "store"
        publish_panel(source, _JANUARY, _cohort(_JANUARY))
        root = tmp_path / "adopted"
        adopt_bundle_generation(root, source, expected_current=None)

        pointers = sorted(
            path.relative_to(root).as_posix()
            for path in (root / "lake" / "pointers").rglob("*")
            if path.is_file()
        )

        assert pointers == ["lake/pointers/calibration/current.json"]

    def test_a_cohort_with_no_row_is_a_published_state_not_an_absent_one(
        self, tmp_path: Path
    ) -> None:
        publish_panel(tmp_path, _JANUARY, _cohort(_JANUARY))
        publish_forward(tmp_path, _JANUARY, [])

        assert _dataset_ref(tmp_path, CALIBRATION_FORWARD.name).rows == 0
        assert read_forward(tmp_path, date.fromisoformat(_JANUARY)) == []

    def test_an_empty_panel_is_computed_and_reads_as_empty(self, tmp_path: Path) -> None:
        publish_panel(tmp_path, _JANUARY, [])

        assert has_cohort(tmp_path, date.fromisoformat(_JANUARY)) is True
        assert published_cohorts(tmp_path) == [date.fromisoformat(_JANUARY)]
        assert read_panel(tmp_path, date.fromisoformat(_JANUARY)) == []

    def test_a_partial_dataset_publication_never_moves_the_bundle_pointer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        publish_panel(tmp_path, _JANUARY, _cohort(_JANUARY))
        before = _bundle_pointer(tmp_path).current
        original = store._publish_cohort

        def fail_diagnostics(*args: object, **kwargs: object) -> None:
            if kwargs.get("dataset") == CALIBRATION_DIAGNOSTICS:
                raise CalibrationLakeError("injected partial publication")
            original(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(store, "_publish_cohort", fail_diagnostics)
        with pytest.raises(CalibrationCacheError, match="injected partial"):
            publish_panel(tmp_path, _FEBRUARY, _cohort(_FEBRUARY))

        assert _bundle_pointer(tmp_path).current == before
        assert published_cohorts(tmp_path) == [date.fromisoformat(_JANUARY)]

        monkeypatch.undo()
        publish_panel(tmp_path, _FEBRUARY, _cohort(_FEBRUARY))
        assert published_cohorts(tmp_path) == [
            date.fromisoformat(_JANUARY),
            date.fromisoformat(_FEBRUARY),
        ]

    def test_a_reader_keeps_the_bundle_it_resolved_at_start(self, tmp_path: Path) -> None:
        publish_panel(tmp_path, _JANUARY, _cohort(_JANUARY))
        fixed = resolve_calibration_bundle(tmp_path)

        publish_panel(tmp_path, _FEBRUARY, _cohort(_FEBRUARY))

        assert published_cohorts(tmp_path, bundle=fixed) == [date.fromisoformat(_JANUARY)]
        assert read_panel(tmp_path, date.fromisoformat(_JANUARY), bundle=fixed)
        with pytest.raises(CalibrationCacheError, match="partial"):
            read_panel(tmp_path, date.fromisoformat(_FEBRUARY), bundle=fixed)

    def test_a_second_local_writer_fails_explicitly(self, tmp_path: Path) -> None:
        errors: list[BaseException] = []

        def contender() -> None:
            try:
                publish_panel(tmp_path, _JANUARY, _cohort(_JANUARY))
            except BaseException as exc:
                errors.append(exc)

        with lake_writer_lock(tmp_path):
            thread = threading.Thread(target=contender)
            thread.start()
            thread.join(timeout=5)
        assert not thread.is_alive()
        assert len(errors) == 1
        assert "publication lock" in str(errors[0])
        assert not (tmp_path / current_calibration_bundle_pointer_key()).exists()


class TestFailClose:
    def test_a_build_from_another_transform_is_refused(self, tmp_path: Path) -> None:
        publish_panel(tmp_path, _JANUARY, _cohort(_JANUARY))

        with pytest.raises(CalibrationLakeError, match="different transform"):
            require_build_inputs(
                _manifest(tmp_path, CALIBRATION_PANEL.name),
                dataset=CALIBRATION_PANEL,
                cache_schema_version="0" * 16,
            )

    def test_a_build_bound_to_another_release_is_refused(self, tmp_path: Path) -> None:
        publish_panel(tmp_path, _JANUARY, _cohort(_JANUARY))

        with pytest.raises(CalibrationLakeError, match="not built from"):
            require_build_inputs(
                _manifest(tmp_path, CALIBRATION_PANEL.name),
                dataset=CALIBRATION_PANEL,
                cache_schema_version=store.CACHE_SCHEMA_VERSIONS[CALIBRATION_PANEL.name],
                source_release_id="release-that-was-not-used",
            )

    def test_a_published_object_whose_bytes_changed_is_refused(self, tmp_path: Path) -> None:
        publish_panel(tmp_path, _JANUARY, _cohort(_JANUARY))
        objects = sorted((tmp_path / "lake" / "l2").rglob("*.parquet"))
        assert objects
        objects[0].write_bytes(objects[0].read_bytes() + b"tamper")

        with pytest.raises(CalibrationCacheError, match="cache is invalid"):
            read_panel(tmp_path, date.fromisoformat(_JANUARY))

    def test_a_missing_object_is_refused(self, tmp_path: Path) -> None:
        publish_panel(tmp_path, _JANUARY, _cohort(_JANUARY))
        for path in sorted((tmp_path / "lake" / "l2").rglob("*.parquet")):
            path.unlink()

        with pytest.raises(CalibrationCacheError, match="cache is invalid"):
            read_panel(tmp_path, date.fromisoformat(_JANUARY))

    def test_an_invalid_pointer_is_refused_rather_than_read_as_absent(self, tmp_path: Path) -> None:
        publish_panel(tmp_path, _JANUARY, _cohort(_JANUARY))
        pointer_path = tmp_path / current_calibration_bundle_pointer_key()
        payload = json.loads(pointer_path.read_text(encoding="utf-8"))
        del payload["current"]["manifest_sha256"]
        pointer_path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(CalibrationCacheError, match="pointer is unreadable"):
            read_panel(tmp_path, date.fromisoformat(_JANUARY))

    def test_duplicate_panel_primary_key_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(CalibrationCacheError, match="duplicate primary key"):
            publish_panel(tmp_path, _JANUARY, _cohort(_JANUARY, tickers=("1301", "1301")))

    def test_duplicate_forward_primary_key_is_refused(self, tmp_path: Path) -> None:
        publish_panel(tmp_path, _JANUARY, _cohort(_JANUARY, tickers=("1301",)))
        rows = [
            {"ticker": "1301", "horizon": "1y", "status": "unresolved_future_horizon"},
            {"ticker": "1301", "horizon": "1y", "status": "unresolved_future_horizon"},
        ]
        with pytest.raises(CalibrationCacheError, match="duplicate primary key"):
            publish_forward(tmp_path, _JANUARY, rows)


class TestRebuild:
    def test_a_deleted_store_rebuilds_from_the_same_inputs(self, tmp_path: Path) -> None:
        """The cache is derived, so deleting it must lose nothing but time."""

        import shutil

        publish_panel(tmp_path, _JANUARY, _cohort(_JANUARY))
        publish_forward(
            tmp_path,
            _JANUARY,
            [{"ticker": "1301", "horizon": "1y", "price_return": 0.2, "status": "resolved"}],
        )
        before_panel = read_panel(tmp_path, date.fromisoformat(_JANUARY))
        before_forward = read_forward(tmp_path, date.fromisoformat(_JANUARY))
        before_meta = read_panel_meta(tmp_path, date.fromisoformat(_JANUARY))

        shutil.rmtree(tmp_path / "lake")
        publish_panel(tmp_path, _JANUARY, _cohort(_JANUARY))
        publish_forward(
            tmp_path,
            _JANUARY,
            [{"ticker": "1301", "horizon": "1y", "price_return": 0.2, "status": "resolved"}],
        )

        assert read_panel(tmp_path, date.fromisoformat(_JANUARY)) == before_panel
        assert read_forward(tmp_path, date.fromisoformat(_JANUARY)) == before_forward
        assert read_panel_meta(tmp_path, date.fromisoformat(_JANUARY)) == before_meta

    def test_force_adoption_failure_keeps_the_previous_generation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        current = tmp_path / "current"
        generated = tmp_path / "generated"
        publish_panel(current, _JANUARY, _cohort(_JANUARY))
        publish_panel(generated, _FEBRUARY, _cohort(_FEBRUARY))
        expected = current_bundle_ref(current)
        original = store.write_bytes_atomic

        def fail_pointer(path: Path, payload: bytes) -> None:
            if path == current / current_calibration_bundle_pointer_key():
                raise OSError("injected pointer failure")
            original(path, payload)

        monkeypatch.setattr(store, "write_bytes_atomic", fail_pointer)
        with pytest.raises(OSError, match="injected pointer"):
            adopt_bundle_generation(current, generated, expected_current=expected)

        assert current_bundle_ref(current) == expected
        assert read_panel(current, date.fromisoformat(_JANUARY))

    def test_force_adoption_keeps_the_previous_bundle_as_rollback(self, tmp_path: Path) -> None:
        current = tmp_path / "current"
        generated = tmp_path / "generated"
        publish_panel(current, _JANUARY, _cohort(_JANUARY))
        publish_panel(generated, _FEBRUARY, _cohort(_FEBRUARY))
        previous = current_bundle_ref(current)

        adopted = adopt_bundle_generation(current, generated, expected_current=previous)

        pointer = _bundle_pointer(current)
        assert pointer.current.bundle_id == adopted.bundle_id
        assert pointer.previous == previous
        assert read_panel(current, date.fromisoformat(_FEBRUARY))

    def test_adoption_installs_the_change_and_reuses_what_the_store_already_holds(
        self, tmp_path: Path
    ) -> None:
        """Adoption I/O follows the generation's closure, not the store's whole history.

        A generation is built by hard-linking the store into it, so a carried object
        arrives already sharing an inode with the one in the store. Installing it again,
        or hashing it to discover it is the same, would make every update cost the size
        of everything ever published.
        """

        current = tmp_path / "current"
        for month in (_JANUARY, _FEBRUARY, _MARCH):
            publish_panel(current, month, _cohort(month))
        generated = tmp_path / "generated"
        copytree(current, generated, copy_function=link)
        publish_panel(generated, _APRIL, _cohort(_APRIL))
        previous = current_bundle_ref(current)

        report = adopt_bundle_generation(current, generated, expected_current=previous)

        assert report.reused_objects > 0
        assert report.installed_objects < report.closure_objects
        assert report.installed_bytes < report.hashed_bytes
        assert read_panel(current, date.fromisoformat(_APRIL))
        assert read_panel(current, date.fromisoformat(_JANUARY))

    def test_adoption_refuses_a_generation_whose_cohort_source_is_gone(
        self, tmp_path: Path
    ) -> None:
        """Losing the input a cohort was built from is invisible on the read path.

        Its rows read back perfectly; the loss only surfaces later, when someone tries
        to reproduce, pin, or publish the cohort. So the pointer switch is the last
        place that can still refuse it.
        """

        current = tmp_path / "current"
        generated = tmp_path / "generated"
        publish_panel(current, _JANUARY, _cohort(_JANUARY))
        archived, source = _retained_calibration_source(generated)
        store.write_panel(
            generated,
            date.fromisoformat(_FEBRUARY),
            (),
            PanelDiagnostics(
                asof=_FEBRUARY,
                rules_hash="abc123",
                universe_size=0,
                population_size=0,
                candidates=0,
                evidence_candidates=0,
                bars_tickers_not_in_master=0,
                effective_bars_start="2020-01-01",
                effective_fin_start="2020-01-01",
                bars_window_clamped=False,
                fin_window_clamped=False,
                population_per_trailing_nonnull=0,
                population_pbr_nonnull=0,
                population_ocf_yield_nonnull=0,
                population_per_trailing_exact=0,
            ),
            source=source,
            input_cutoff=date.fromisoformat(_FEBRUARY),
            producer_commit="a" * 40,
        )
        expected = current_bundle_ref(current)
        archived.unlink()

        with pytest.raises(CalibrationLakeError, match="source does not resolve"):
            adopt_bundle_generation(current, generated, expected_current=expected)

        assert current_bundle_ref(current) == expected


class TestRetention:
    def test_the_current_and_previous_build_are_never_candidates(self, tmp_path: Path) -> None:
        publish_panel(tmp_path, _JANUARY, _cohort(_JANUARY))
        first = _dataset_ref(tmp_path, CALIBRATION_PANEL.name)
        publish_panel(tmp_path, _FEBRUARY, _cohort(_FEBRUARY))
        second = _dataset_ref(tmp_path, CALIBRATION_PANEL.name)

        plan = plan_gc(tmp_path, now=datetime.now(UTC) + timedelta(days=400))

        assert first.build_id != second.build_id
        assert second.manifest_key in plan.reachable
        assert first.manifest_key in plan.reachable
        assert first.manifest_key not in {item.key for item in plan.candidates}

    def test_calibration_builds_without_a_bundle_pointer_stop_the_sweep(
        self, tmp_path: Path
    ) -> None:
        """A missing root over published builds is a loss, not an empty store.

        Reading it as "nothing is published" would make every build it protects look
        unreferenced, which is the one way a reachability sweep deletes live data.
        """

        publish_panel(tmp_path, _JANUARY, _cohort(_JANUARY))
        (tmp_path / current_calibration_bundle_pointer_key()).unlink()

        plan = plan_gc(tmp_path, now=datetime.now(UTC) + timedelta(days=400))

        assert current_calibration_bundle_pointer_key() in plan.unresolved_roots
        with pytest.raises(LakeRetentionError, match="root is unresolved"):
            apply_gc(tmp_path, plan, plan_hash=plan.plan_hash)

    def test_a_build_older_than_current_and_previous_becomes_a_candidate(
        self, tmp_path: Path
    ) -> None:
        for asof in (_JANUARY, _FEBRUARY, "2026-03-31"):
            publish_panel(tmp_path, asof, _cohort(asof))
        current = _dataset_ref(tmp_path, CALIBRATION_PANEL.name)

        plan = plan_gc(tmp_path, now=datetime.now(UTC) + timedelta(days=400))

        candidates = {item.key for item in plan.candidates}
        assert candidates  # the first build is neither current nor previous
        assert current.manifest_key not in candidates
        assert (
            plan.plan_hash
            == plan_gc(tmp_path, now=datetime.now(UTC) + timedelta(days=400)).plan_hash
        )

    def test_a_pinned_build_survives_the_sweep(self, tmp_path: Path) -> None:
        publish_panel(tmp_path, _JANUARY, _cohort(_JANUARY))
        first = _bundle_pointer(tmp_path).current
        for asof in (_FEBRUARY, "2026-03-31"):
            publish_panel(tmp_path, asof, _cohort(asof))
        create_pin(
            tmp_path,
            pin_id="pin-adopted-cohort",
            target_kind="calibration_bundle",
            target_id=first.bundle_id,
            reason="adopted as calibration evidence",
            owner="owner",
        )

        plan = plan_gc(
            tmp_path,
            now=datetime.now(UTC) + timedelta(days=400),
        )

        assert first.manifest_key in plan.reachable
        assert first.manifest_key not in {item.key for item in plan.candidates}
        pins = read_pins(tmp_path)
        assert [pin.pin_id for pin in pins] == ["pin-adopted-cohort"]
        assert pins[0].manifest_key == first.manifest_key
        assert pins[0].manifest_sha256 == first.manifest_sha256
        assert len(list((tmp_path / "lake/audit/pins").glob("*.json"))) == 1

    def test_removing_a_pin_returns_its_target_to_the_sweep(self, tmp_path: Path) -> None:
        publish_panel(tmp_path, _JANUARY, _cohort(_JANUARY))
        first = _bundle_pointer(tmp_path).current
        for asof in (_FEBRUARY, "2026-03-31"):
            publish_panel(tmp_path, asof, _cohort(asof))
        create_pin(
            tmp_path,
            pin_id="pin-adopted-cohort",
            target_kind="calibration_bundle",
            target_id=first.bundle_id,
            reason="adopted as calibration evidence",
            owner="owner",
        )

        assert remove_pin(tmp_path, pin_id="pin-adopted-cohort") is True
        assert not (tmp_path / pin_key(pin_id="pin-adopted-cohort")).exists()
        audit_actions = {
            json.loads(path.read_text(encoding="utf-8"))["action"]
            for path in (tmp_path / "lake/audit/pins").glob("*.json")
        }
        assert audit_actions == {"create", "remove_requested", "remove"}
        plan = plan_gc(
            tmp_path,
            now=datetime.now(UTC) + timedelta(days=400),
        )
        assert first.manifest_key in {item.key for item in plan.candidates}

    def test_a_pin_target_that_does_not_exist_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(LakeRetentionError, match="target manifest is missing"):
            create_pin(
                tmp_path,
                pin_id="pin-no-dataset",
                target_kind="calibration_bundle",
                target_id="some-bundle",
                reason="reason",
                owner="owner",
            )

    def test_a_pin_target_with_a_missing_object_is_refused(self, tmp_path: Path) -> None:
        publish_panel(tmp_path, _JANUARY, _cohort(_JANUARY))
        target = _bundle_pointer(tmp_path).current
        next((tmp_path / "lake/l2").rglob("*.parquet")).unlink()

        with pytest.raises(LakeRetentionError, match="target closure is incomplete"):
            create_pin(
                tmp_path,
                pin_id="pin-incomplete-bundle",
                target_kind="calibration_bundle",
                target_id=target.bundle_id,
                reason="must not pin a partial graph",
                owner="test",
            )

    def test_applying_a_plan_requires_the_hash_that_plan_produced(self, tmp_path: Path) -> None:
        for asof in (_JANUARY, _FEBRUARY, "2026-03-31"):
            publish_panel(tmp_path, asof, _cohort(asof))
        plan = plan_gc(
            tmp_path,
            now=datetime.now(UTC) + timedelta(days=400),
        )

        with pytest.raises(LakeRetentionError, match="plan hash does not match"):
            apply_gc(tmp_path, plan, plan_hash="0" * 64)

        assert apply_gc(tmp_path, plan, plan_hash=plan.plan_hash) == ()
        second = plan_gc(
            tmp_path,
            now=plan.evaluated_at + timedelta(days=8),
        )
        deleted = apply_gc(tmp_path, second, plan_hash=second.plan_hash)
        assert set(deleted) == {item.key for item in plan.candidates}
        # The current cohort still reads after the sweep.
        assert read_panel(tmp_path, date.fromisoformat("2026-03-31"))

    def test_a_sweep_refuses_to_delete_while_a_root_is_unresolved(self, tmp_path: Path) -> None:
        publish_panel(tmp_path, _JANUARY, _cohort(_JANUARY))
        reference = _dataset_ref(tmp_path, CALIBRATION_PANEL.name)
        (tmp_path / reference.manifest_key).unlink()

        plan = plan_gc(tmp_path)

        assert plan.unresolved_roots
        with pytest.raises(LakeRetentionError, match="root is unresolved"):
            apply_gc(tmp_path, plan, plan_hash=plan.plan_hash)

    def test_a_reachable_candidate_loses_its_old_second_sweep_mark(self, tmp_path: Path) -> None:
        publish_panel(tmp_path, _JANUARY, _cohort(_JANUARY))
        first = _bundle_pointer(tmp_path).current
        for asof in (_FEBRUARY, "2026-03-31", "2026-04-30"):
            publish_panel(tmp_path, asof, _cohort(asof))
        marked_plan = plan_gc(tmp_path, now=datetime.now(UTC) + timedelta(days=400))
        assert first.manifest_key in {item.key for item in marked_plan.candidates}
        assert apply_gc(tmp_path, marked_plan, plan_hash=marked_plan.plan_hash) == ()
        marker = (
            tmp_path
            / "lake/retention/marks"
            / f"{hashlib.sha256(first.manifest_key.encode()).hexdigest()}.json"
        )
        assert marker.is_file()

        create_pin(
            tmp_path,
            pin_id="first-bundle-live-again",
            target_kind="calibration_bundle",
            target_id=first.bundle_id,
            reason="regression fixture",
            owner="test",
        )
        fresh = plan_gc(tmp_path, now=marked_plan.evaluated_at + timedelta(days=8))
        apply_gc(tmp_path, fresh, plan_hash=fresh.plan_hash)

        assert (tmp_path / first.manifest_key).is_file()
        assert not marker.exists()

    def test_a_pointer_update_after_planning_refuses_the_sweep(self, tmp_path: Path) -> None:
        for asof in (_JANUARY, _FEBRUARY, "2026-03-31"):
            publish_panel(tmp_path, asof, _cohort(asof))
        plan = plan_gc(tmp_path, now=datetime.now(UTC) + timedelta(days=400))

        publish_panel(tmp_path, "2026-04-30", _cohort("2026-04-30"))

        with pytest.raises(LakeRetentionError, match="generation or candidate identity changed"):
            apply_gc(tmp_path, plan, plan_hash=plan.plan_hash)

    def test_a_candidate_replaced_after_planning_refuses_the_sweep(self, tmp_path: Path) -> None:
        for asof in (_JANUARY, _FEBRUARY, "2026-03-31"):
            publish_panel(tmp_path, asof, _cohort(asof))
        plan = plan_gc(tmp_path, now=datetime.now(UTC) + timedelta(days=400))
        candidate = next(item for item in plan.candidates if item.key.endswith(".json"))
        path = tmp_path / candidate.key
        path.write_bytes(path.read_bytes() + b" ")

        with pytest.raises(LakeRetentionError, match="generation or candidate identity changed"):
            apply_gc(tmp_path, plan, plan_hash=plan.plan_hash)


class TestLegacyParity:
    """A cohort in the retired CSV layout must read as the same rows a build holds."""

    def _write_legacy(self, root: Path, asof: str, rows: list[PanelRow]) -> None:
        root.mkdir(parents=True, exist_ok=True)
        names = [field.name for field in dc_fields(PanelRow)]
        with (root / f"panel-{asof}.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(names)
            for row in rows:
                writer.writerow([_encode(getattr(row, name)) for name in names])
        (root / f"panel-{asof}.meta.yaml").write_text(
            f"asof: '{asof}'\nrules_hash: abc123\n", encoding="utf-8"
        )

    def _write_legacy_forward(self, root: Path, asof: str, rows: list[ForwardReturnRow]) -> None:
        names = [field.name for field in dc_fields(ForwardReturnRow)]
        with (root / f"forward-{asof}.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(names)
            for row in rows:
                writer.writerow([_encode(getattr(row, name)) for name in names])

    def test_the_same_cohort_reads_identically_from_both_representations(
        self, tmp_path: Path
    ) -> None:
        published = tmp_path / "published"
        legacy = tmp_path / "legacy"
        publish_panel(published, _JANUARY, _cohort(_JANUARY))
        forward_rows = [
            {"ticker": "1301", "horizon": "1y", "price_return": 0.2, "status": "resolved"},
            {"ticker": "7203", "horizon": "1y", "status": "unresolved_future_horizon"},
        ]
        publish_forward(published, _JANUARY, forward_rows)
        asof = date.fromisoformat(_JANUARY)

        self._write_legacy(legacy, _JANUARY, read_panel(published, asof))
        self._write_legacy_forward(legacy, _JANUARY, read_forward(published, asof))

        assert read_legacy_panel(legacy, asof) == read_panel(published, asof)
        assert read_legacy_forward(legacy, asof) == read_forward(published, asof)
        assert legacy_cohorts(legacy) == published_cohorts(published)
        assert (
            read_legacy_panel_meta(legacy, asof)["rules_hash"]
            == read_panel_meta(published, asof)["rules_hash"]
        )

    def test_a_legacy_cohort_missing_a_column_the_contract_reads_is_refused(
        self, tmp_path: Path
    ) -> None:
        published = tmp_path / "published"
        legacy = tmp_path / "legacy"
        publish_panel(published, _JANUARY, _cohort(_JANUARY))
        asof = date.fromisoformat(_JANUARY)
        self._write_legacy(legacy, _JANUARY, read_panel(published, asof))
        path = legacy / f"panel-{_JANUARY}.csv"
        lines = path.read_text(encoding="utf-8").splitlines()
        kept = [name for name in lines[0].split(",") if name != "er_annual"]
        path.write_text(",".join(kept) + "\n", encoding="utf-8")

        with pytest.raises(CalibrationCacheError, match="missing er_annual"):
            read_legacy_panel(legacy, asof)

    def test_compatible_legacy_history_migrates_with_exact_parity(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(legacy_csv_module, "verified_git_commit", lambda: "a" * 40)
        published = tmp_path / "published"
        legacy = tmp_path / "legacy"
        destination = tmp_path / "destination"
        report = tmp_path / "migration.json"
        publish_panel(published, _JANUARY, _cohort(_JANUARY))
        publish_forward(
            published,
            _JANUARY,
            [{"ticker": "1301", "horizon": "1y", "status": "unresolved_future_horizon"}],
        )
        asof = date.fromisoformat(_JANUARY)
        panel = read_panel(published, asof)
        forward = read_forward(published, asof)
        self._write_legacy(legacy, _JANUARY, panel)
        self._write_legacy_forward(legacy, _JANUARY, forward)
        (legacy / f"panel-{_JANUARY}.meta.yaml").write_text(
            yaml.safe_dump(read_panel_meta(published, asof), sort_keys=False),
            encoding="utf-8",
        )
        (legacy / "calibration.meta.yaml").write_text(
            yaml.safe_dump({"cache_schema_version": CACHE_SCHEMA_VERSION}),
            encoding="utf-8",
        )
        legacy_panel = legacy / f"panel-{_JANUARY}.csv"
        original_panel_bytes = legacy_panel.read_bytes()
        original_install = legacy_csv_module._install_archived_copy

        def mutate_after_capture(target: Path, source: Path, *, expected_sha256: str) -> None:
            original_install(target, source, expected_sha256=expected_sha256)
            if source == legacy_panel:
                source.write_bytes(original_panel_bytes.replace(b"1301", b"9999", 1))

        monkeypatch.setattr(legacy_csv_module, "_install_archived_copy", mutate_after_capture)

        result = migrate_legacy_calibration(legacy, destination, report_path=report)

        assert result["status"] == "migrated"
        assert len(str(result["producer_git_commit"])) == 40
        assert read_panel(destination, asof) == panel
        assert read_forward(destination, asof) == forward
        assert read_panel_meta(destination, asof) == read_panel_meta(published, asof)
        assert all(result["parity"].values())
        assert report.is_file()
        assert not list(tmp_path.glob(".destination.migration.*"))
        legacy_panel.write_bytes(b"mutated after migration")
        archived_panel = (
            destination / "lake/l2/calibration-legacy" / str(result["input_id"]) / legacy_panel.name
        )
        assert archived_panel.read_bytes() == original_panel_bytes

    def test_incompatible_legacy_archive_is_outside_the_l2_gc_domain(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(legacy_csv_module, "verified_git_commit", lambda: "a" * 40)
        legacy = tmp_path / "legacy"
        legacy.mkdir()
        for name, payload in (
            (f"panel-{_JANUARY}.csv", b"legacy-panel"),
            (f"panel-{_JANUARY}.meta.yaml", b"rules_hash: old\n"),
            (f"forward-{_JANUARY}.csv", b"legacy-forward"),
            ("calibration.meta.yaml", b"cache_schema_version: incompatible\n"),
        ):
            (legacy / name).write_bytes(payload)
        destination = tmp_path / "destination"

        result = migrate_legacy_calibration(
            legacy,
            destination,
            report_path=tmp_path / "report.json",
        )
        plan = plan_gc(destination, now=datetime.now(UTC) + timedelta(days=400))

        assert result["status"] == "archived_incompatible"
        archived_prefix = f"lake/l2/calibration-legacy/{result['input_id']}/"
        assert not any(item.key.startswith(archived_prefix) for item in plan.candidates)

    def test_post_commit_report_failure_is_retryable_as_committed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(legacy_csv_module, "verified_git_commit", lambda: "a" * 40)
        published = tmp_path / "published"
        legacy = tmp_path / "legacy"
        destination = tmp_path / "destination"
        report_path = tmp_path / "migration.json"
        publish_panel(published, _JANUARY, _cohort(_JANUARY))
        publish_forward(published, _JANUARY, [])
        asof = date.fromisoformat(_JANUARY)
        self._write_legacy(legacy, _JANUARY, read_panel(published, asof))
        self._write_legacy_forward(legacy, _JANUARY, read_forward(published, asof))
        (legacy / f"panel-{_JANUARY}.meta.yaml").write_text(
            yaml.safe_dump(read_panel_meta(published, asof), sort_keys=False),
            encoding="utf-8",
        )
        (legacy / "calibration.meta.yaml").write_text(
            yaml.safe_dump({"cache_schema_version": CACHE_SCHEMA_VERSION}),
            encoding="utf-8",
        )
        original_write = legacy_csv_module.write_bytes_atomic

        def fail_report(path: Path, payload: bytes) -> None:
            if path == report_path:
                raise OSError("injected report failure")
            original_write(path, payload)

        monkeypatch.setattr(legacy_csv_module, "write_bytes_atomic", fail_report)
        committed = migrate_legacy_calibration(
            legacy,
            destination,
            report_path=report_path,
        )

        assert committed["status"] == "migrated"
        assert committed["completion"] == "committed_with_warnings"
        assert read_panel(destination, asof)

        monkeypatch.setattr(legacy_csv_module, "write_bytes_atomic", original_write)
        retried = migrate_legacy_calibration(
            legacy,
            destination,
            report_path=report_path,
        )
        assert retried["status"] == "already_migrated"
        assert retried["completion"] == "committed"
        assert report_path.is_file()

    def test_pin_rejects_a_bundle_the_reader_schema_rejects(self, tmp_path: Path) -> None:
        publish_panel(tmp_path, _JANUARY, _cohort(_JANUARY))
        target = _bundle_pointer(tmp_path).current
        path = tmp_path / target.manifest_key
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["cohorts"] = {"not-a-date": "not-an-inventory"}
        path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(LakeRetentionError, match="target manifest is invalid"):
            create_pin(
                tmp_path,
                pin_id="pin-invalid-bundle",
                target_kind="calibration_bundle",
                target_id=target.bundle_id,
                reason="must remain reader-valid",
                owner="test",
            )

    def test_legacy_migration_rejects_a_symlink_input(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(legacy_csv_module, "verified_git_commit", lambda: "a" * 40)
        legacy = tmp_path / "legacy"
        legacy.mkdir()
        external = tmp_path / "outside.csv"
        external.write_text("secret", encoding="utf-8")
        (legacy / f"panel-{_JANUARY}.csv").symlink_to(external)
        (legacy / f"panel-{_JANUARY}.meta.yaml").write_text(
            "rules_hash: abc123\n", encoding="utf-8"
        )
        (legacy / f"forward-{_JANUARY}.csv").write_text("asof\n", encoding="utf-8")
        (legacy / "calibration.meta.yaml").write_text(
            yaml.safe_dump({"cache_schema_version": CACHE_SCHEMA_VERSION}),
            encoding="utf-8",
        )

        with pytest.raises(CalibrationCacheError, match="escapes its root"):
            migrate_legacy_calibration(
                legacy,
                tmp_path / "destination",
                report_path=tmp_path / "report.json",
            )


def _encode(value: object) -> str:
    if value is None:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, float):
        return repr(value)
    return str(value)


class TestReviewRegressions:
    """Each test here fixes a way the store could have lost or misreported data."""

    def test_a_dataset_the_caller_did_not_name_is_still_a_retention_root(
        self, tmp_path: Path
    ) -> None:
        """The store's own bundle pointer is the root, so no caller can forget one.

        Naming datasets used to be how a caller asserted roots, which made a forgotten
        name look like an unreferenced build — the same failure as an unreadable pointer,
        arriving from the other side. There is now one root and nothing to name.
        """

        publish_panel(tmp_path, _JANUARY, _cohort(_JANUARY))
        publish_forward(
            tmp_path,
            _JANUARY,
            [{"ticker": "1301", "horizon": "1y", "price_return": 0.2, "status": "resolved"}],
        )
        live = {
            item.key
            for name in (
                CALIBRATION_PANEL.name,
                CALIBRATION_DIAGNOSTICS.name,
                CALIBRATION_FORWARD.name,
            )
            for partition in _manifest(tmp_path, name).partitions
            for item in partition.objects
        }

        plan = plan_gc(tmp_path, now=datetime.now(UTC) + timedelta(days=400))

        candidates = {item.key for item in plan.candidates}
        assert not (live & candidates)
        assert plan.roots == (current_calibration_bundle_pointer_key(),)

    def test_canonical_objects_with_no_pointer_leave_the_plan_unappliable(
        self, tmp_path: Path
    ) -> None:
        target = (
            tmp_path
            / "lake/l1/canonical/jquants.daily_bars/contract=v1/year=2026/month=1"
            / f"part-{'a' * 64}.parquet"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"object")

        plan = plan_gc(tmp_path, now=datetime.now(UTC) + timedelta(days=400))

        assert plan.unresolved_roots == ("lake/pointers/l1/current.json",)
        with pytest.raises(LakeRetentionError, match="root is unresolved"):
            apply_gc(tmp_path, plan, plan_hash=plan.plan_hash)

    def test_a_map_column_round_trips_as_a_mapping(self, tmp_path: Path) -> None:
        """A populated map read back as pairs would be measured as empty."""

        counts = {"delisting_pending": 7, "trust_bank": 3}
        publish_panel(tmp_path, _JANUARY, _cohort(_JANUARY), exclusion_counts=counts)

        meta = read_panel_meta(tmp_path, date.fromisoformat(_JANUARY))

        assert meta["policy_exclusion_reason_counts"] == counts
        assert isinstance(meta["policy_exclusion_reason_counts"], dict)

    def test_a_build_from_another_transform_is_never_carried_into_a_new_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Measurement rules changed, so the old cohorts must not be re-stamped."""

        publish_panel(tmp_path, _JANUARY, _cohort(_JANUARY))
        monkeypatch.setitem(store.CACHE_SCHEMA_VERSIONS, CALIBRATION_PANEL.name, "0" * 16)

        with pytest.raises(CalibrationCacheError, match="different transform"):
            publish_panel(tmp_path, _FEBRUARY, _cohort(_FEBRUARY))

    def test_a_broken_store_raises_instead_of_reading_as_empty(self, tmp_path: Path) -> None:
        publish_panel(tmp_path, _JANUARY, _cohort(_JANUARY))
        for path in sorted((tmp_path / "lake" / "l2").rglob("*.parquet")):
            path.unlink()

        with pytest.raises(CalibrationCacheError):
            has_cohort(tmp_path, date.fromisoformat(_JANUARY))
        with pytest.raises(CalibrationCacheError, match="cache is invalid"):
            published_cohorts(tmp_path)

    def test_an_empty_store_still_reads_as_empty(self, tmp_path: Path) -> None:
        assert published_cohorts(tmp_path) == []
        assert has_cohort(tmp_path, date.fromisoformat(_JANUARY)) is False

    def test_a_manifest_replaced_under_the_pointer_is_refused(self, tmp_path: Path) -> None:
        publish_panel(tmp_path, _JANUARY, _cohort(_JANUARY))
        reference = _dataset_ref(tmp_path, CALIBRATION_PANEL.name)
        path = tmp_path / reference.manifest_key
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["producer_git_commit"] = "f" * 40
        path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(CalibrationCacheError, match="bundle dataset digest differs"):
            read_panel(tmp_path, date.fromisoformat(_JANUARY))

    def test_a_forward_cohort_the_build_has_not_reached_is_partial_not_empty(
        self, tmp_path: Path
    ) -> None:
        publish_panel(tmp_path, _JANUARY, _cohort(_JANUARY))
        publish_panel(tmp_path, _FEBRUARY, _cohort(_FEBRUARY))
        publish_forward(
            tmp_path,
            _JANUARY,
            [{"ticker": "1301", "horizon": "1y", "price_return": 0.2, "status": "resolved"}],
        )

        assert read_forward(tmp_path, date.fromisoformat(_JANUARY)) != []
        with pytest.raises(CalibrationCacheError, match="partial"):
            read_forward(tmp_path, date.fromisoformat(_FEBRUARY))

    def test_a_carried_partition_whose_object_vanished_stops_the_build(
        self, tmp_path: Path
    ) -> None:
        publish_panel(tmp_path, _JANUARY, _cohort(_JANUARY))
        january = [
            item
            for partition in _manifest(tmp_path, CALIBRATION_PANEL.name).partitions
            for item in partition.objects
        ]
        (tmp_path / january[0].key).unlink()

        with pytest.raises(CalibrationCacheError, match="published object is missing"):
            publish_panel(tmp_path, _FEBRUARY, _cohort(_FEBRUARY))

    def test_a_partition_holds_the_same_bytes_whatever_order_its_cohorts_arrived(
        self, tmp_path: Path
    ) -> None:
        """Two cohorts in one month must not depend on which was published first."""

        first = tmp_path / "first"
        second = tmp_path / "second"
        early, late = "2026-01-15", _JANUARY
        publish_panel(first, early, _cohort(early, tickers=("1301",)))
        publish_panel(first, late, _cohort(late, tickers=("7203",)))
        publish_panel(second, late, _cohort(late, tickers=("7203",)))
        publish_panel(second, early, _cohort(early, tickers=("1301",)))

        keys = [
            {
                item.key
                for p in _manifest(root, CALIBRATION_PANEL.name).partitions
                for item in p.objects
            }
            for root in (first, second)
        ]
        assert keys[0] == keys[1]


class TestSemanticIdentity:
    """What a build has to be identified by before another build may carry it."""

    _ENGINE_ROOT = Path(lake_module.__file__).resolve().parents[2]

    def _fingerprint_with_changed_file(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        dataset: object,
        relative_path: str,
    ) -> str:
        target = self._ENGINE_ROOT / relative_path
        assert target.is_file(), relative_path
        real = lake_module.sha256_file
        monkeypatch.setattr(
            lake_module,
            "sha256_file",
            lambda path: "0" * 64 if path == target else real(path),
        )
        return transform_fingerprint(dataset, cache_schema_version="contract")  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "relative_path",
        [
            "screening/candidate_build.py",
            "screening/selection/__init__.py",
            "screening/sqlite_reader.py",
            "screening/universe.py",
            "market/store.py",
            # Reached only through other modules. A hand-written list of dependencies is
            # exactly where these go missing: nothing in the panel names them, and each
            # one decides what a stored value is.
            "foundation/coerce.py",
            "screening/metric_quality.py",
            "screening/capital_control.py",
            "market/sqlite/convert.py",
        ],
    )
    def test_a_panel_dependency_change_changes_the_panel_fingerprint(
        self, monkeypatch: pytest.MonkeyPatch, relative_path: str
    ) -> None:
        baseline = transform_fingerprint(CALIBRATION_PANEL, cache_schema_version="contract")

        changed = self._fingerprint_with_changed_file(
            monkeypatch, dataset=CALIBRATION_PANEL, relative_path=relative_path
        )

        assert changed != baseline

    @pytest.mark.parametrize(
        "relative_path",
        ["market/bars.py", "market/benchmark.py", "screening/calibration/horizons.py"],
    )
    def test_a_forward_dependency_change_changes_the_forward_fingerprint(
        self, monkeypatch: pytest.MonkeyPatch, relative_path: str
    ) -> None:
        baseline = transform_fingerprint(CALIBRATION_FORWARD, cache_schema_version="contract")

        changed = self._fingerprint_with_changed_file(
            monkeypatch, dataset=CALIBRATION_FORWARD, relative_path=relative_path
        )

        assert changed != baseline

    def test_a_module_outside_the_closure_leaves_the_fingerprint_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rebuilding every cohort for an unrelated change is its own failure."""

        baseline = transform_fingerprint(CALIBRATION_PANEL, cache_schema_version="contract")

        changed = self._fingerprint_with_changed_file(
            monkeypatch, dataset=CALIBRATION_PANEL, relative_path="macro/__init__.py"
        )

        assert changed == baseline

    def test_the_two_datasets_do_not_invalidate_each_other(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """How an outcome is observed and how a panel is screened are separate closures.

        They share the entry-lag contract and nothing else, so a change to either
        producer leaves the other's published months carryable.
        """

        panel_baseline = transform_fingerprint(CALIBRATION_PANEL, cache_schema_version="contract")
        forward_baseline = transform_fingerprint(
            CALIBRATION_FORWARD, cache_schema_version="contract"
        )

        panel_after_forward_change = self._fingerprint_with_changed_file(
            monkeypatch,
            dataset=CALIBRATION_PANEL,
            relative_path="screening/calibration/forward.py",
        )
        monkeypatch.undo()
        forward_after_panel_change = self._fingerprint_with_changed_file(
            monkeypatch,
            dataset=CALIBRATION_FORWARD,
            relative_path="screening/calibration/panel.py",
        )

        assert panel_after_forward_change == panel_baseline
        assert forward_after_panel_change == forward_baseline

    def test_a_forward_column_does_not_invalidate_published_panel_months(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Adding a forward column must not force 81 panel cohorts to be rebuilt.

        A rebuild costs hours and hundreds of megabytes; spending them on a change that
        cannot move a panel value also dilutes what "this needs rebuilding" means. The
        screening thresholds and the valuation revision decide panel and diagnostics
        values; they do not decide when a position was entered or what it paid.
        """

        panel_before = transform_fingerprint(
            CALIBRATION_PANEL,
            cache_schema_version=store.CACHE_SCHEMA_VERSIONS[CALIBRATION_PANEL.name],
        )
        forward_before = transform_fingerprint(
            CALIBRATION_FORWARD,
            cache_schema_version=store.CACHE_SCHEMA_VERSIONS[CALIBRATION_FORWARD.name],
        )
        monkeypatch.setattr(
            store, "FORWARD_FIELD_NAMES", (*store.FORWARD_FIELD_NAMES, "an_added_column")
        )
        versions = store._derive_cache_schema_version()

        assert (
            versions[CALIBRATION_PANEL.name] == store.CACHE_SCHEMA_VERSIONS[CALIBRATION_PANEL.name]
        )
        assert (
            versions[CALIBRATION_FORWARD.name]
            != store.CACHE_SCHEMA_VERSIONS[CALIBRATION_FORWARD.name]
        )
        assert (
            transform_fingerprint(
                CALIBRATION_PANEL, cache_schema_version=versions[CALIBRATION_PANEL.name]
            )
            == panel_before
        )
        assert (
            transform_fingerprint(
                CALIBRATION_FORWARD, cache_schema_version=versions[CALIBRATION_FORWARD.name]
            )
            != forward_before
        )

    def test_a_changed_row_type_without_a_contract_bump_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The contract version in the object key is what an outside reader can trust.

        Leaving it at v1 while the columns change means the same prefix addresses two
        shapes. This repository's reader still refuses the mismatch because it compares
        the whole Arrow schema, but nothing outside it would.
        """

        assert lake_module.verify_l2_schema_signatures() == []

        monkeypatch.setitem(
            lake_module._RECORDED_SCHEMA_SIGNATURES,
            (CALIBRATION_FORWARD.name, CALIBRATION_FORWARD.contract_version),
            "0" * 64,
        )

        drifted = lake_module.verify_l2_schema_signatures()

        assert [message.split(":")[0] for message in drifted] == [CALIBRATION_FORWARD.name]

    def test_a_dependency_a_producer_starts_importing_joins_the_closure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A new import must change the identity without anyone maintaining a list.

        This is the failure a named list cannot prevent: a listed module starts
        importing a helper, rows change, and the fingerprint says nothing did.
        """

        engine_root = self._ENGINE_ROOT
        entry = tmp_path / "producer.py"
        helper = tmp_path / "helper.py"
        helper.write_text("VALUE = 1\n", encoding="utf-8")
        entry.write_text("VALUE = 0\n", encoding="utf-8")
        monkeypatch.setattr(lake_module, "_ENGINE_ROOT", tmp_path)
        monkeypatch.setitem(
            lake_module._DATASET_ENTRY_MODULES, lake_module.PANEL_DATASET, "producer.py"
        )
        monkeypatch.setattr(lake_module, "_WRITER_MODULES", ())
        lake_module._semantic_closure.cache_clear()
        before = set(lake_module._semantic_implementation_digests(CALIBRATION_PANEL))

        entry.write_text("from baibai_engine.helper import VALUE\n", encoding="utf-8")
        lake_module._semantic_closure.cache_clear()
        after = set(lake_module._semantic_implementation_digests(CALIBRATION_PANEL))
        lake_module._semantic_closure.cache_clear()

        assert before == {"producer.py"}
        assert after == {"producer.py", "helper.py"}
        assert engine_root.is_dir()

    def test_the_forward_observation_policy_is_part_of_the_forward_identity(self) -> None:
        baseline = transform_fingerprint(CALIBRATION_FORWARD, cache_schema_version="contract")

        without_exits = transform_fingerprint(
            CALIBRATION_FORWARD,
            cache_schema_version="contract",
            forward_policy=ForwardObservationPolicy(use_control_event_exits=False),
        )

        assert without_exits != baseline

    def test_the_forward_observation_policy_does_not_move_the_panel_identity(self) -> None:
        """A comparison run must not invalidate months whose rows it cannot change."""

        baseline = transform_fingerprint(CALIBRATION_PANEL, cache_schema_version="contract")

        under_other_rules = transform_fingerprint(
            CALIBRATION_PANEL,
            cache_schema_version="contract",
            forward_policy=ForwardObservationPolicy(use_control_event_exits=False),
        )

        assert under_other_rules == baseline

    def test_a_store_refuses_to_hold_two_forward_observation_policies(self, tmp_path: Path) -> None:
        publish_panel(tmp_path, _JANUARY, _cohort(_JANUARY))
        publish_forward(tmp_path, _JANUARY, _forward_rows(_JANUARY))

        with pytest.raises(CalibrationCacheError, match="different transform"):
            publish_forward(
                tmp_path,
                _JANUARY,
                _forward_rows(_JANUARY),
                forward_policy=ForwardObservationPolicy(use_control_event_exits=False),
            )

        assert store.store_forward_policy(tmp_path) == DEFAULT_FORWARD_OBSERVATION_POLICY

    def test_a_store_states_the_rules_its_forward_rows_were_observed_under(
        self, tmp_path: Path
    ) -> None:
        policy = ForwardObservationPolicy(use_control_event_exits=False)
        publish_panel(tmp_path, _JANUARY, _cohort(_JANUARY), forward_policy=policy)
        publish_forward(tmp_path, _JANUARY, _forward_rows(_JANUARY), forward_policy=policy)

        assert store.store_forward_policy(tmp_path) == policy
        assert read_forward(tmp_path, date.fromisoformat(_JANUARY))
