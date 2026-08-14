from __future__ import annotations

import io
import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from tests.helpers.screening_run_fixture import ASOF, build_screening_market_store

from baibai_engine.foundation.time import JST
from baibai_engine.market.lake import models as lake_models
from baibai_engine.market.lake.duck import lake_session
from baibai_engine.market.lake.keys import current_l1_pointer_key
from baibai_engine.market.lake.objects import LakeObjectCache, LocalMirrorSource
from baibai_engine.market.lake.projection import ProjectionError, build_projection
from baibai_engine.market.lake.reader import resolve_current_release
from baibai_engine.market.lake.release import (
    L1ReleasePointer,
    canonical_json_bytes,
    create_l1_release,
)
from baibai_engine.market.lake.writer import capture_legacy_sqlite_snapshot, export_legacy_sqlite
from baibai_engine.screening.config import ScreeningConfig
from baibai_engine.screening.lake_shadow import (
    LakeShadowError,
    ParityDifference,
    ScreeningParityReport,
    ScreeningSideResult,
    build_shadow_store,
    compare_sides,
    run_lake_shadow_parity,
)
from baibai_engine.screening.rule_config import DEFAULT_RULES_PATH, load_screening_rules

_COMMIT = "d" * 40
_CREATED_AT = datetime(2026, 6, 30, 12, 0, tzinfo=UTC)
_NOW = datetime(2026, 6, 30, 18, 0, tzinfo=JST)
_RELEASE = "release-shadow"


@pytest.fixture(scope="module")
def frozen_lake(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """One frozen store, its release, and a projection of that release."""

    datasets = tuple(
        item.model_copy(
            update={
                "coverage_start_on_or_before": date.max,
                "minimum_rows": 1,
                "minimum_population_count": 1,
            }
        )
        for item in lake_models.PILOT_RELEASE_POLICY.datasets
    )
    patcher = pytest.MonkeyPatch()
    patcher.setattr(
        lake_models,
        "PILOT_RELEASE_POLICY",
        lake_models.PILOT_RELEASE_POLICY.model_copy(update={"datasets": datasets}),
    )

    root = tmp_path_factory.mktemp("shadow")
    sqlite_path = build_screening_market_store(root / "market.sqlite")
    mirror = root / "mirror"
    snapshot = capture_legacy_sqlite_snapshot(
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        snapshot_id="snapshot-shadow",
    )
    manifests = [
        export_legacy_sqlite(
            dataset_name=name,
            sqlite_path=sqlite_path,
            mirror_root=mirror,
            producer_git_commit=_COMMIT,
            source_snapshot_ref=snapshot.ref,
            build_id=f"build-{name.replace('.', '-')}",
            created_at=_CREATED_AT,
        ).manifest_path
        for name in ("jquants.daily_bars", "jquants.short_sale_reports")
    ]
    manifest_path, _release = create_l1_release(
        dataset_manifest_paths=manifests,
        mirror_root=mirror,
        release_id=_RELEASE,
        created_at=_CREATED_AT,
    )
    import hashlib

    pointer = L1ReleasePointer(
        release_id=_RELEASE,
        manifest_key=manifest_path.relative_to(mirror).as_posix(),
        manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    )
    pointer_path = mirror / current_l1_pointer_key()
    pointer_path.parent.mkdir(parents=True, exist_ok=True)
    pointer_path.write_bytes(canonical_json_bytes(pointer))

    cache = LakeObjectCache(root=mirror, source=LocalMirrorSource(mirror))
    with lake_session() as session:
        build_projection(
            session,
            release=resolve_current_release(cache.source),
            cache=cache,
            destination=root / "projection.sqlite",
            dataset_names=("jquants.daily_bars", "jquants.short_sale_reports"),
            producer_git_commit=_COMMIT,
            built_at=_CREATED_AT,
        )
    try:
        yield root
    finally:
        patcher.undo()


class TestShadowStore:
    def test_migrated_tables_come_from_the_release_and_the_rest_stay_legacy(
        self, frozen_lake: Path, tmp_path: Path
    ) -> None:
        report = build_shadow_store(
            legacy_sqlite=frozen_lake / "market.sqlite",
            projection=frozen_lake / "projection.sqlite",
            destination=tmp_path / "shadow.sqlite",
        )

        assert report.release_id == _RELEASE
        assert report.release_sourced_tables == (
            "jquants_daily_bars",
            "jquants_short_sale_reports",
        )
        assert "jquants_fin_summaries" in report.legacy_sourced_tables
        assert "jquants_daily_bars" not in report.legacy_sourced_tables
        with (
            sqlite3.connect(report.path) as shadow,
            sqlite3.connect(frozen_lake / "market.sqlite") as legacy,
        ):
            for table in report.release_sourced_tables:
                assert (
                    shadow.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                    == legacy.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                )

    def test_rows_are_value_identical_to_the_legacy_store(
        self, frozen_lake: Path, tmp_path: Path
    ) -> None:
        report = build_shadow_store(
            legacy_sqlite=frozen_lake / "market.sqlite",
            projection=frozen_lake / "projection.sqlite",
            destination=tmp_path / "shadow.sqlite",
        )
        query = (
            "SELECT ticker, traded_at, open, high, low, close, volume, turnover_value, "
            "adjustment_close, adjustment_factor FROM jquants_daily_bars "
            "ORDER BY ticker, traded_at"
        )

        with (
            sqlite3.connect(report.path) as shadow,
            sqlite3.connect(frozen_lake / "market.sqlite") as legacy,
        ):
            assert shadow.execute(query).fetchall() == legacy.execute(query).fetchall()

    def test_an_absent_projection_is_refused(self, frozen_lake: Path, tmp_path: Path) -> None:
        with pytest.raises(ProjectionError, match="absent or unreadable"):
            build_shadow_store(
                legacy_sqlite=frozen_lake / "market.sqlite",
                projection=tmp_path / "missing.sqlite",
                destination=tmp_path / "shadow.sqlite",
            )

    def test_a_failed_build_publishes_nothing(self, frozen_lake: Path, tmp_path: Path) -> None:
        destination = tmp_path / "shadow.sqlite"

        with pytest.raises(ProjectionError):
            build_shadow_store(
                legacy_sqlite=frozen_lake / "market.sqlite",
                projection=tmp_path / "missing.sqlite",
                destination=destination,
            )

        assert not destination.exists()
        assert not list(tmp_path.glob("*.part"))


class TestParityComparison:
    def _side(
        self, *, close: float = 100.0, order: tuple[str, ...] = ("9001", "9002")
    ) -> ScreeningSideResult:
        return ScreeningSideResult(
            run={
                "run_id": "screening-20260630",
                "run_revision_id": f"run-{close}",
                "candidates": [
                    {"ticker": ticker, "metrics": {"close": close if ticker == "9001" else 900.0}}
                    for ticker in order
                ],
            },
            selection={
                "selection_id": f"selection-{close}",
                "recommendations": [
                    {"ticker": ticker, "rank": index + 1} for index, ticker in enumerate(order)
                ],
            },
        )

    def test_identical_sides_report_no_difference_despite_new_identifiers(self) -> None:
        assert compare_sides(legacy=self._side(), lake=self._side()) == []

    def test_a_changed_metric_is_reported_on_the_candidate_and_the_metric_scope(self) -> None:
        differences = compare_sides(legacy=self._side(), lake=self._side(close=101.0))

        assert ParityDifference("metric", "9001", "close", "100.0", "101.0") in differences
        assert any(item.scope == "candidate" and item.key == "9001" for item in differences)

    def test_a_changed_order_is_reported_even_when_every_value_matches(self) -> None:
        differences = compare_sides(legacy=self._side(), lake=self._side(order=("9002", "9001")))

        assert [item for item in differences if item.field == "order"] != []

    def test_a_comparison_with_no_candidate_on_either_side_is_not_a_match(self) -> None:
        """Two empty runs agree trivially; reporting that as a match proves nothing."""

        report = ScreeningParityReport(
            asof="2026-06-30",
            release_id=_RELEASE,
            release_sourced_tables=("jquants_daily_bars",),
            legacy_sourced_tables=("jquants_fin_summaries",),
            candidates_compared=0,
            selection_entries_compared=0,
            differences=(),
            difference_counts={"candidate": 0, "metric": 0, "selection": 0},
            total_differences=0,
        )

        assert report.compared_nothing is True
        assert report.matched is False
        assert report.as_dict()["compared_nothing"] is True

    def test_a_candidate_present_on_one_side_only_is_reported_as_absent(self) -> None:
        legacy = self._side()
        lake = ScreeningSideResult(
            run={**legacy.run, "candidates": legacy.run["candidates"][:1]},  # type: ignore[index]
            selection=legacy.selection,
        )

        differences = compare_sides(legacy=legacy, lake=lake)

        assert any(item.key == "9002" and item.lake == "<absent>" for item in differences)


class TestShadowRun:
    def test_screening_candidate_metric_and_selection_all_match(
        self, frozen_lake: Path, tmp_path: Path
    ) -> None:
        config = ScreeningConfig(
            jquants_api_key="offline-cache-only",
            sqlite_cache_dir=frozen_lake,
            cache_dir=tmp_path / "cache",
        )

        report = run_lake_shadow_parity(
            asof=ASOF,
            legacy_sqlite=frozen_lake / "market.sqlite",
            projection=frozen_lake / "projection.sqlite",
            workspace=tmp_path / "workspace",
            config=config,
            rules=load_screening_rules(DEFAULT_RULES_PATH),
            now=_NOW,
            app_db_path=tmp_path / "app.sqlite",
            select_top=10,
            stdout=io.StringIO(),
        )

        assert report.differences == ()
        assert report.matched is True
        assert report.candidates_compared == 2
        # A selection with nothing in it would compare vacuously.
        assert report.selection_entries_compared > 0
        assert report.release_id == _RELEASE
        assert report.release_sourced_tables == (
            "jquants_daily_bars",
            "jquants_short_sale_reports",
        )
        # The blocker is stated by the report itself, not left to a reader to infer.
        assert "jquants_fin_summaries" in report.legacy_sourced_tables

    def test_a_release_whose_rows_drifted_makes_the_comparison_fail(
        self, frozen_lake: Path, tmp_path: Path
    ) -> None:
        projection = tmp_path / "drifted.sqlite"
        projection.write_bytes((frozen_lake / "projection.sqlite").read_bytes())
        with sqlite3.connect(projection) as connection:
            connection.execute(
                "UPDATE jquants_daily_bars SET close = close * 2 WHERE traded_at >= ?",
                ((ASOF.replace(day=1)).isoformat(),),
            )
        config = ScreeningConfig(
            jquants_api_key="offline-cache-only",
            sqlite_cache_dir=frozen_lake,
            cache_dir=tmp_path / "cache",
        )

        report = run_lake_shadow_parity(
            asof=ASOF,
            legacy_sqlite=frozen_lake / "market.sqlite",
            projection=projection,
            workspace=tmp_path / "workspace",
            config=config,
            rules=load_screening_rules(DEFAULT_RULES_PATH),
            now=_NOW,
            app_db_path=tmp_path / "app.sqlite",
            select_top=10,
            stdout=io.StringIO(),
        )

        assert report.matched is False
        assert report.difference_counts["candidate"] > 0
        assert report.difference_counts["metric"] > 0
        assert report.total_differences >= len(report.differences)

    def test_an_incomplete_release_fails_instead_of_refilling_from_a_provider(
        self, frozen_lake: Path, tmp_path: Path
    ) -> None:
        """A gap in the release must stop the run, not be repaired behind its back.

        Providers run cache-only for both sides. If one could fetch, a release that
        was missing rows would produce a matching comparison for the wrong reason.
        """

        projection = tmp_path / "incomplete.sqlite"
        projection.write_bytes((frozen_lake / "projection.sqlite").read_bytes())
        with sqlite3.connect(projection) as connection:
            connection.execute(
                "DELETE FROM jquants_daily_bars WHERE traded_at >= ?",
                ((ASOF.replace(month=1, day=1)).isoformat(),),
            )
        config = ScreeningConfig(
            jquants_api_key="offline-cache-only",
            sqlite_cache_dir=frozen_lake,
            cache_dir=tmp_path / "cache",
        )

        with pytest.raises(LakeShadowError, match="screening run failed"):
            run_lake_shadow_parity(
                asof=ASOF,
                legacy_sqlite=frozen_lake / "market.sqlite",
                projection=projection,
                workspace=tmp_path / "workspace",
                config=config,
                rules=load_screening_rules(DEFAULT_RULES_PATH),
                now=_NOW,
                app_db_path=tmp_path / "app.sqlite",
                select_top=10,
                stdout=io.StringIO(),
            )
