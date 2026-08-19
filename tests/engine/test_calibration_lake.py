from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from os import link
from pathlib import Path
from shutil import copytree

import pytest
from pydantic import TypeAdapter, ValidationError
from tests.helpers.calibration_store import (
    publish_forward,
    publish_panel,
    synthetic_calibration_source,
)
from tests.helpers.l1_release import stored_release_source

from baibai_engine.market.lake import models as lake_models
from baibai_engine.market.lake import sources as sources_module
from baibai_engine.market.lake.keys import (
    calibration_bundle_manifest_key,
    current_calibration_bundle_pointer_key,
    release_manifest_key,
)
from baibai_engine.market.lake.models import (
    CalibrationBundleManifest,
    CalibrationDatasetRef,
    CohortInventoryEntry,
    DatasetManifest,
    L1ReleaseSourceRef,
    MeasurementPolicyRef,
    SourceRef,
    require_calibration_generation,
    source_assurance,
)
from baibai_engine.market.lake.retention import (
    LakeRetentionError,
    apply_gc,
    lake_writer_lock,
    plan_gc,
)
from baibai_engine.screening.calibration import lake as lake_module
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
    FixedCalibrationBundle,
    load_manifest,
    require_build_inputs,
    require_l2_dataset,
    transform_fingerprint,
)
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


def _repoint_bundle(root: Path, payload: dict[str, object]) -> None:
    """Serve a bundle manifest an alternate writer could have produced.

    Every digest edge is closed, so nothing about the store is malformed on its own
    terms — which is the point: what the reader has to refuse is a graph that is
    internally consistent and still says two different things.
    """

    manifest_payload = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    key = calibration_bundle_manifest_key(bundle_id=str(payload["bundle_id"]))
    path = root / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(manifest_payload)
    pointer = CalibrationBundlePointer(
        current=lake_models.CalibrationBundleRef(
            bundle_id=str(payload["bundle_id"]),
            manifest_key=key,
            manifest_sha256=hashlib.sha256(manifest_payload).hexdigest(),
        ),
        previous=None,
    )
    (root / current_calibration_bundle_pointer_key()).write_bytes(
        json.dumps(pointer.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
        + b"\n"
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
        original = lake_module.semantic_source_digest
        monkeypatch.setattr(
            lake_module,
            "semantic_source_digest",
            lambda path: "0" * 64 if path.name == "panel.py" else original(path),
        )
        assert same != transform_fingerprint(
            CALIBRATION_PANEL, cache_schema_version=CACHE_SCHEMA_VERSION
        )


def _release_source() -> L1ReleaseSourceRef:
    release_id = "20260130T000000Z-release"
    return L1ReleaseSourceRef(
        kind="l1_release",
        source_id=release_id,
        key=release_manifest_key(release_id=release_id),
        sha256="b" * 64,
        manifest_version=1,
    )


class TestImmutableBuilds:
    def test_a_bundle_that_mixes_measurement_policies_cannot_be_parsed(
        self, tmp_path: Path
    ) -> None:
        """The invariant belongs to the wire format, not to whichever writer assembled it.

        The standard assembler refuses the mixture, but a migration tool or an alternate
        producer writing the same JSON would not — and the reader on the other side has
        no way to notice, because every field it checks agrees.
        """

        publish_panel(tmp_path, _JANUARY, _cohort(_JANUARY), rules_hash="rules-one")
        publish_panel(tmp_path, _FEBRUARY, _cohort(_FEBRUARY), rules_hash="rules-one")
        mixed = {}
        for name, manifest in resolve_calibration_bundle(tmp_path).datasets.items():
            inventory = dict(manifest.cohort_inventory)
            entry = inventory[_FEBRUARY]
            inventory[_FEBRUARY] = entry.model_copy(
                update={
                    "measurement_policy": entry.measurement_policy.model_copy(
                        update={"rules_hash": "rules-two"}
                    )
                }
            )
            mixed[name] = manifest.model_copy(update={"cohort_inventory": inventory})

        with pytest.raises(ValueError, match="mix measurement policies"):
            require_calibration_generation(mixed)

    def test_a_cohort_whose_roles_disagree_about_the_rules_cannot_be_parsed(
        self, tmp_path: Path
    ) -> None:
        publish_panel(tmp_path, _JANUARY, _cohort(_JANUARY), rules_hash="rules-one")
        manifests = dict(resolve_calibration_bundle(tmp_path).datasets)
        forward = manifests[CALIBRATION_FORWARD.name]
        inventory = dict(forward.cohort_inventory)
        entry = inventory[_JANUARY]
        inventory[_JANUARY] = entry.model_copy(
            update={
                "measurement_policy": entry.measurement_policy.model_copy(
                    update={"rules_hash": "rules-two"}
                )
            }
        )
        manifests[CALIBRATION_FORWARD.name] = forward.model_copy(
            update={"cohort_inventory": inventory}
        )

        with pytest.raises(ValueError, match="disagree about the rules"):
            require_calibration_generation(manifests)

    def test_a_run_given_a_fixed_generation_never_consults_the_pointer_again(
        self, tmp_path: Path
    ) -> None:
        """Fixing a generation has to fix everything the run reads, or it fixes nothing.

        An evaluation resolves the bundle once and hands it to every reader. If a reader
        re-resolves current for any part of its answer, the run's inputs depend on when
        it ran relative to somebody else's publication: the same fixed bundle reads on
        one attempt and fails on the next. Deleting the pointer is the sharpest form of
        the question — a pinned study has to survive it.
        """

        publish_panel(tmp_path, _JANUARY, _cohort(_JANUARY))
        publish_forward(tmp_path, _JANUARY, _forward_rows(_JANUARY))
        first = resolve_calibration_bundle(tmp_path)
        january = date.fromisoformat(_JANUARY)
        expected_panel = read_panel(tmp_path, january, bundle=first)
        expected_forward = read_forward(tmp_path, january, bundle=first)

        # current moves on, and then stops existing at all.
        publish_panel(tmp_path, _FEBRUARY, _cohort(_FEBRUARY))
        assert read_panel(tmp_path, january, bundle=first) == expected_panel
        (tmp_path / current_calibration_bundle_pointer_key()).unlink()

        assert read_panel(tmp_path, january, bundle=first) == expected_panel
        assert read_forward(tmp_path, january, bundle=first) == expected_forward
        assert read_panel_meta(tmp_path, january, bundle=first)["rules_hash"]

    def test_asking_whether_a_cohort_exists_reads_one_generation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The inventory that answers and the rows that confirm it are one generation.

        ``has_cohort`` checks the entry and then reads the payload to tell a published
        cohort from a claim about one. Resolving current a second time for the payload
        makes the two halves of one question describe two different generations, and a
        publication landing in between turns "yes" into a false or into an error.
        """

        publish_panel(tmp_path, _JANUARY, _cohort(_JANUARY))
        january = date.fromisoformat(_JANUARY)
        assert has_cohort(tmp_path, january) is True

        resolve = store._fixed_bundle
        resolved = 0

        def once(root: Path) -> FixedCalibrationBundle | None:
            nonlocal resolved
            resolved += 1
            if resolved > 1:
                raise AssertionError("current was resolved twice to answer one question")
            return resolve(root)

        monkeypatch.setattr(store, "_fixed_bundle", once)
        assert has_cohort(tmp_path, january) is True

    def test_one_datasets_contract_move_does_not_unresolve_the_others(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Resolving a generation is structural; the version belongs to whoever decodes.

        This is the reason the contract version is per dataset. A forward contract bump
        must not make the panel — whose columns did not move and whose objects this code
        decodes perfectly — unreadable, because that turns every dataset's evolution into
        a rebuild of all three.
        """

        publish_panel(tmp_path, _JANUARY, _cohort(_JANUARY))
        publish_forward(tmp_path, _JANUARY, _forward_rows(_JANUARY))
        january = date.fromisoformat(_JANUARY)
        expected_panel = read_panel(tmp_path, january)

        upgraded = replace(CALIBRATION_FORWARD, contract_version=2)
        monkeypatch.setitem(lake_module.L2_DATASETS, CALIBRATION_FORWARD.name, upgraded)
        monkeypatch.setattr(store, "CALIBRATION_FORWARD", upgraded)

        assert read_panel(tmp_path, january) == expected_panel
        assert read_panel_meta(tmp_path, january)["rules_hash"]
        with pytest.raises(CalibrationCacheError, match="accepts only v2"):
            read_forward(tmp_path, january)

    def test_a_bundle_cannot_restate_a_store_wide_compatibility_value(self, tmp_path: Path) -> None:
        # The field was removed rather than verified: nothing derived it from the
        # dataset manifests, so an alternate writer could record any value and no reader
        # would be wrong. The wire has to refuse it rather than carry it unread.
        publish_panel(tmp_path, _JANUARY, _cohort(_JANUARY))
        payload = json.loads(
            (tmp_path / _bundle_pointer(tmp_path).current.manifest_key).read_bytes()
        )
        payload["cache_schema_version"] = "0" * 16

        with pytest.raises(ValidationError, match="cache_schema_version"):
            CalibrationBundleManifest.model_validate_json(json.dumps(payload))

    def test_a_bundle_that_hides_a_cohort_its_datasets_hold_is_refused(
        self, tmp_path: Path
    ) -> None:
        """What the bundle publishes and what its datasets hold have to be one set.

        Checking only that every listed cohort is present leaves the other direction
        open: a generation can then serve January while its three datasets hold January
        and February, so the February objects are reachable through the bundle, counted
        against it by retention, and described by nothing in it.
        """

        publish_panel(tmp_path, _JANUARY, _cohort(_JANUARY))
        publish_panel(tmp_path, _FEBRUARY, _cohort(_FEBRUARY))
        manifests = dict(resolve_calibration_bundle(tmp_path).datasets)
        panel = manifests[CALIBRATION_PANEL.name]
        manifests[CALIBRATION_PANEL.name] = panel.model_copy(
            update={
                "cohort_inventory": {
                    asof: entry
                    for asof, entry in panel.cohort_inventory.items()
                    if asof != _FEBRUARY
                }
            }
        )

        with pytest.raises(ValueError, match="publish different cohorts"):
            require_calibration_generation(manifests)

    def test_a_rewritten_panel_does_not_keep_the_outcomes_of_the_one_it_replaced(
        self, tmp_path: Path
    ) -> None:
        """A forward cohort observes the names its panel selected, and only those.

        The rules identity does not catch a replacement: the same rules over corrected
        inputs select a different cross-section. Carrying the stored outcomes across
        would publish a readable generation whose two halves describe different sets of
        names, and nothing before evaluation would look at it.
        """

        publish_panel(tmp_path, _JANUARY, _cohort(_JANUARY, ("1301", "7203")))
        publish_forward(tmp_path, _JANUARY, _forward_rows(_JANUARY))
        assert read_forward(tmp_path, date.fromisoformat(_JANUARY))

        publish_panel(tmp_path, _JANUARY, _cohort(_JANUARY, ("1301",)))

        assert (
            resolve_calibration_bundle(tmp_path).cohorts[_JANUARY].forward.status == "not_computed"
        )
        with pytest.raises(CalibrationCacheError, match="partial"):
            read_forward(tmp_path, date.fromisoformat(_JANUARY))

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
        cohort = bundle.cohorts[_JANUARY]

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

    def test_a_cohort_may_name_an_l1_release_beside_the_snapshot_it_read(
        self, tmp_path: Path
    ) -> None:
        """The release says where the rows can be read again; the snapshot says which
        rows were read. A cohort states both, and stating only the release would drop
        the generation the build actually saw."""

        del tmp_path
        policy = MeasurementPolicyRef(
            rules_hash="abc123", panel_variant="production", production_authority=True
        )
        snapshot = synthetic_calibration_source(captured_on=date.fromisoformat(_JANUARY))

        entry = CohortInventoryEntry(
            status="empty",
            rows=0,
            sources=(snapshot, _release_source()),
            input_cutoff=date.fromisoformat(_JANUARY),
            measurement_policy=policy,
        )

        assert [item.kind for item in entry.sources] == ["sqlite_snapshot", "l1_release"]

    def test_a_cohort_stating_only_a_release_is_refused(self, tmp_path: Path) -> None:
        """A release names a generation, not the read. Without the sealed snapshot the
        cohort cannot say which bytes it saw, and the input-cutoff check that binds the
        two would have nothing to compare against."""

        del tmp_path
        policy = MeasurementPolicyRef(
            rules_hash="abc123", panel_variant="production", production_authority=True
        )

        with pytest.raises(ValidationError, match="sealed store generation"):
            CohortInventoryEntry(
                status="empty",
                rows=0,
                sources=(_release_source(),),
                input_cutoff=date.fromisoformat(_JANUARY),
                measurement_policy=policy,
            )

    def test_a_release_is_still_not_a_partition_source(self, tmp_path: Path) -> None:
        """`SourceRef` is what one build records about the generation it read, and a
        build reads a sealed store. Widening the cohort union does not widen that one."""

        del tmp_path
        with pytest.raises(ValidationError):
            TypeAdapter(SourceRef).validate_python(_release_source().model_dump(mode="python"))

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

        bundle = resolve_calibration_bundle(tmp_path).cohorts[_JANUARY]

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

    def test_a_superseded_build_manifest_is_left_where_it_was(self, tmp_path: Path) -> None:
        """Publishing does not delete: what a generation replaces is left for the collector."""

        publish_panel(tmp_path, _JANUARY, _cohort(_JANUARY))
        first = _dataset_ref(tmp_path, CALIBRATION_PANEL.name)

        publish_panel(tmp_path, _FEBRUARY, _cohort(_FEBRUARY))
        second = _dataset_ref(tmp_path, CALIBRATION_PANEL.name)

        assert second.build_id != first.build_id
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

    def test_adopting_a_generation_serializes_on_the_same_lock(self, tmp_path: Path) -> None:
        """Adoption is the compare-and-set, so it cannot depend on its caller taking it.

        It reads the current pointer, installs a closure, and writes the pointer last.
        Two callers that read the same current would both pass the comparison, both
        install, and the later write would win — with the earlier caller told it
        succeeded and its generation never served.
        """

        current = tmp_path / "current"
        generated = tmp_path / "generated"
        publish_panel(generated, _JANUARY, _cohort(_JANUARY))
        errors: list[BaseException] = []

        def contender() -> None:
            try:
                adopt_bundle_generation(current, generated, expected_current=None)
            except BaseException as exc:
                errors.append(exc)

        with lake_writer_lock(current):
            thread = threading.Thread(target=contender)
            thread.start()
            thread.join(timeout=5)
        assert not thread.is_alive()
        assert len(errors) == 1
        assert "publication lock" in str(errors[0])
        assert not (current / current_calibration_bundle_pointer_key()).exists()

        adopt_bundle_generation(current, generated, expected_current=None)
        assert resolve_calibration_bundle(current).cohorts


class TestSourceAssurance:
    """What each lineage kind lets someone do, which is what authority is decided on."""

    def test_an_upstream_input_the_lake_keeps_is_the_rebuildable_level(
        self, tmp_path: Path
    ) -> None:
        # The level production authority requires. Nothing a cohort may name reaches it
        # yet — `CohortSourceRef` admits only sealed snapshots, whose bytes the lake does
        # not keep — so the positive case is stated here rather than left to be
        # discovered when L1 releases join the union and the gate turns out never to
        # have had a pass.
        assert source_assurance((_release_source(),)) == "rebuildable_input"

    def test_a_generation_the_lake_did_not_keep_is_trace_only(self) -> None:
        assert source_assurance((synthetic_calibration_source(),)) == "trace_only"

    def test_naming_no_source_is_the_weakest_claim_rather_than_no_claim(self) -> None:
        # An empty tuple satisfies "every source is retained" vacuously, which would make
        # a cohort that states no lineage at all the strongest one in the store.
        assert source_assurance(()) == "trace_only"


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

    def test_one_source_is_verified_once_however_many_cohorts_name_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verification cost must follow how much source there is, not how often it is named.

        A retained source is one object that every partition built from it points at.
        Paying per reference turns one object and a 121 month release into that object's
        size times the number of partitions that name it, every run.
        """

        archived, source = stored_release_source(tmp_path)
        hashed: list[str] = []
        real = sources_module.sha256_file
        monkeypatch.setattr(
            sources_module,
            "sha256_file",
            lambda path: (hashed.append(path.name), real(path))[1],
        )

        with sources_module.verified_source_scope():
            for _ in range(5):
                sources_module.resolve_source_ref(tmp_path, source)

        assert hashed.count(archived.name) == 1

    def test_a_second_reference_claiming_the_same_identity_is_still_resolved(
        self, tmp_path: Path
    ) -> None:
        """Memoizing keys on the identity the caller asserted, not on the file it found."""

        _, source = stored_release_source(tmp_path)
        other = tmp_path / "other-mirror"
        other.mkdir()

        with sources_module.verified_source_scope():
            sources_module.resolve_source_ref(tmp_path, source)

            with pytest.raises(ValueError, match="does not resolve"):
                sources_module.resolve_source_ref(other, source)


class TestRetention:
    def test_the_current_build_is_kept_and_the_one_it_replaced_is_not(self, tmp_path: Path) -> None:
        """The store keeps what it serves. What it used to serve is not a second answer.

        Objects the current generation still carries stay reachable through it, so a
        superseded manifest becoming a candidate does not take the rows with it.
        """

        publish_panel(tmp_path, _JANUARY, _cohort(_JANUARY))
        first = _dataset_ref(tmp_path, CALIBRATION_PANEL.name)
        publish_panel(tmp_path, _FEBRUARY, _cohort(_FEBRUARY))
        second = _dataset_ref(tmp_path, CALIBRATION_PANEL.name)

        plan = plan_gc(tmp_path, now=datetime.now(UTC) + timedelta(days=400))

        assert first.build_id != second.build_id
        assert second.manifest_key in plan.reachable
        assert first.manifest_key in {item.key for item in plan.candidates}
        assert read_panel(tmp_path, date.fromisoformat(_JANUARY))

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

    def test_applying_a_plan_requires_the_hash_that_plan_produced(self, tmp_path: Path) -> None:
        for asof in (_JANUARY, _FEBRUARY, "2026-03-31"):
            publish_panel(tmp_path, asof, _cohort(asof))
        plan = plan_gc(
            tmp_path,
            now=datetime.now(UTC) + timedelta(days=400),
        )

        with pytest.raises(LakeRetentionError, match="plan hash does not match"):
            apply_gc(tmp_path, plan, plan_hash="0" * 64)

        # The hash the dry run printed is what authorises the deletion, and the sweep
        # finishes in the run the operator started. Nothing is left marked for later.
        deleted = apply_gc(tmp_path, plan, plan_hash=plan.plan_hash)
        assert set(deleted) == {item.key for item in plan.candidates}
        assert not any((tmp_path / key).exists() for key in deleted)
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
        real = lake_module.semantic_source_digest
        monkeypatch.setattr(
            lake_module,
            "semantic_source_digest",
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

    @pytest.mark.parametrize("dataset", [CALIBRATION_PANEL, CALIBRATION_FORWARD])
    def test_the_store_that_orchestrates_a_build_is_not_part_of_its_identity(
        self, monkeypatch: pytest.MonkeyPatch, dataset: object
    ) -> None:
        """The fingerprint answers what produced these bytes, not what published them.

        ``store.py`` holds manifests, pointers, locks, reads and error translation, and
        it hands rows to a writer that builds every payload itself from the row type's
        own field names. Including it made a fix to a read path rewrite the identity of
        all three datasets, which asks for hours of rebuild to produce bytes that were
        already correct — and then makes the next real change indistinguishable from it.
        """

        baseline = transform_fingerprint(dataset, cache_schema_version="contract")  # type: ignore[arg-type]

        changed = self._fingerprint_with_changed_file(
            monkeypatch, dataset=dataset, relative_path="screening/calibration/store.py"
        )

        assert changed == baseline

    @pytest.mark.parametrize("dataset", [CALIBRATION_PANEL, CALIBRATION_FORWARD])
    def test_the_writer_that_turns_rows_into_bytes_stays_part_of_the_identity(
        self, monkeypatch: pytest.MonkeyPatch, dataset: object
    ) -> None:
        baseline = transform_fingerprint(dataset, cache_schema_version="contract")  # type: ignore[arg-type]

        changed = self._fingerprint_with_changed_file(
            monkeypatch, dataset=dataset, relative_path="screening/calibration/lake.py"
        )

        assert changed != baseline

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

    def test_renaming_a_row_type_moves_the_signature_though_no_column_moves(self) -> None:
        """The stamped identity is wire-observable, so the gate has to sign it.

        The reader compares ``baibai.row_type`` exactly and refuses an object that says
        anything else. A rename therefore splits ``contract=v1`` into two mutually
        unreadable halves while leaving every column, key, and partition untouched — the
        change a column-only signature is blind to by construction.
        """

        before = lake_module.schema_signature(CALIBRATION_FORWARD)
        renamed = replace(
            CALIBRATION_FORWARD,
            row_type=type("RenamedForwardRow", (ForwardReturnRow,), {}),
        )

        assert renamed.arrow_schema.equals(CALIBRATION_FORWARD.arrow_schema, check_metadata=False)
        assert lake_module.schema_signature(renamed) != before

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
