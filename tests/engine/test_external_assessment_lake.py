from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from tests.helpers.lake_policy import narrow_release_policy

from baibai_engine.external_assessment.observations.models import (
    AnalystRatingObservation,
    ModelValuationObservation,
)
from baibai_engine.external_assessment.reader import read_observations
from baibai_engine.external_assessment.store import save_observations
from baibai_engine.read_api.external_assessment import external_assessment
from baibai_engine.storage.lake.duck import lake_session
from baibai_engine.storage.lake.hydrate import hydrate_market_store
from baibai_engine.storage.lake.models import PRODUCTION_RELEASE_POLICY
from baibai_engine.storage.lake.objects import LakeObjectCache, LocalMirrorSource
from baibai_engine.storage.lake.reader import resolve_release
from baibai_engine.storage.lake.release import create_l1_release
from baibai_engine.storage.lake.writer import (
    export_legacy_sqlite,
    sealed_sqlite_snapshot,
    validate_legacy_parity,
)
from baibai_engine.storage.observations.jquants import store_jquants_market_calendar
from baibai_engine.storage.observations.schema import SQLiteSchemaError, open_connection

VALUATION = "external_assessment.model_valuation_observations"
RATING = "external_assessment.analyst_rating_observations"
CALENDAR = "jquants.market_calendar"
DATASETS = (VALUATION, RATING, CALENDAR)
RECEIVED = datetime(2026, 9, 30, 14, 59, tzinfo=UTC)


def _publish(path: Path, mirror: Path, datasets: tuple[str, ...], release_id: str):
    with sealed_sqlite_snapshot(sqlite_path=path, mirror_root=mirror) as snapshot:
        exported = [
            export_legacy_sqlite(
                dataset_name=name,
                mirror_root=mirror,
                producer_git_commit="a" * 40,
                source_snapshot=snapshot,
                build_id=f"{release_id}-{index}",
            )
            for index, name in enumerate(datasets)
        ]
    for item in exported:
        validate_legacy_parity(sqlite_path=path, mirror_root=mirror, manifest=item.manifest)
    manifest_path, _ = create_l1_release(
        dataset_manifest_paths=[item.manifest_path for item in exported],
        mirror_root=mirror,
        release_id=release_id,
    )
    source = LocalMirrorSource(mirror)
    release = resolve_release(
        source,
        release_id=release_id,
        manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    )
    return release, LakeObjectCache(root=mirror, source=source), exported


def test_optional_release_roundtrip_preserves_received_history_and_other_dataset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    narrow_release_policy(
        monkeypatch, datasets=DATASETS, carries_history=True, minimum_population_count=None
    )
    path = tmp_path / "market.sqlite"
    open_connection(path).close()
    store_jquants_market_calendar(
        path,
        [
            {"Date": "2026-09-30", "HolidayDivision": "1"},
            {"Date": "2026-10-01", "HolidayDivision": "1"},
        ],
        requested_start=date(2026, 9, 30),
        requested_end=date(2026, 10, 1),
    )
    mirror = tmp_path / "mirror"
    old, old_cache, _ = _publish(path, mirror, (CALENDAR,), "before-retail")
    restored = tmp_path / "restored.sqlite"
    open_connection(restored).close()
    with lake_session() as session:
        hydrate_market_store(
            session, release=old, cache=old_cache, store=restored, dataset_names=(CALENDAR,)
        )
    assert read_observations(restored, "7203", RECEIVED).model_valuations == ()
    assert read_observations(restored, "7203", RECEIVED).analyst_ratings == ()

    observations = []
    for ticker in ("7203", "6758", "9432"):
        for received in (RECEIVED, RECEIVED + timedelta(minutes=2)):
            common = {
                "ticker": ticker,
                "fetched_at_utc": received,
                "source_url": "https://example.test/synthetic",
                "parser_version": "synthetic-1",
            }
            observations.extend(
                [
                    ModelValuationObservation(**common, distributor="monex", consensus_target=100),
                    AnalystRatingObservation(**common, distributor="rakuten", rating=3),
                ]
            )
    assert save_observations(path, observations) == 12
    assert save_observations(path, observations) == 0
    current, cache, exports = _publish(path, mirror, DATASETS, "with-retail")
    for item in exports:
        if item.manifest.dataset != CALENDAR:
            assert item.manifest.totals.rows == 6
            assert [part.values for part in item.manifest.partitions] == [
                {"year": 2026, "month": 9},
                {"year": 2026, "month": 10},
            ]
    with lake_session() as session:
        report = hydrate_market_store(
            session, release=current, cache=cache, store=restored, dataset_names=DATASETS
        )
    assert report.rows == {CALENDAR: 2, VALUATION: 6, RATING: 6}
    for ticker in ("7203", "6758", "9432"):
        cutoff = RECEIVED + timedelta(minutes=2)
        assert read_observations(restored, ticker, cutoff) == read_observations(
            path, ticker, cutoff
        )
        assert read_observations(restored, ticker, RECEIVED).model_valuations[0].previous is None
    with sqlite3.connect(restored) as conn:
        assert conn.execute("SELECT COUNT(*) FROM jquants_market_calendar").fetchone()[0] == 2
    assert save_observations(restored, observations) == 0


def test_read_facade_keeps_latest_null_and_rejects_obsolete_schema(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    common = {
        "ticker": "7203",
        "distributor": "monex",
        "source_url": "https://example.test/synthetic",
        "parser_version": "synthetic-1",
    }
    save_observations(
        path,
        [
            ModelValuationObservation(**common, fetched_at_utc=RECEIVED, consensus_target=100),
            ModelValuationObservation(**common, fetched_at_utc=RECEIVED + timedelta(seconds=1)),
        ],
    )
    cutoff = RECEIVED + timedelta(seconds=1)
    payload = external_assessment(
        path, ticker="7203", cutoff=cutoff, evaluated_at=cutoff
    ).model_dump(mode="json")
    assert payload["model_valuations"][0]["inputs"]["latest"]["consensus_target"] is None
    assert payload["model_valuations"][0]["inputs"]["previous"]["consensus_target"] == 100
    assert payload["model_valuations"][0]["comparisons"][0]["state"] == "missing"
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version=28")
    with pytest.raises(SQLiteSchemaError):
        external_assessment(path, ticker="7203", cutoff=cutoff, evaluated_at=cutoff)


def test_external_assessment_release_policy_is_optional_history_with_unproven_coverage() -> None:
    for name in (VALUATION, RATING):
        policy = next(item for item in PRODUCTION_RELEASE_POLICY.datasets if item.dataset == name)
        assert policy.required is False
        assert policy.carries_history is True
        assert policy.require_complete_coverage is False
