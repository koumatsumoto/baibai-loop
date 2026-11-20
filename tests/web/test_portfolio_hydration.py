"""Standalone serving uses only its market inputs, with the normal lake guards."""

import json
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

import pytest
from tests.helpers.lake_policy import narrow_release_policy
from tests.helpers.outcome_publication import _publication
from tests.helpers.proposal import PUBLISHED, backend_case, reviewed_proposal
from tests.helpers.screening_sqlite import add_source_coverage, seed_daily_bars

from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService
from baibai_engine.portfolio.performance.store import PortfolioOutcomeStore
from baibai_engine.storage.paths import APPLICATION_DB_PATH, MARKET_DB_PATH
from baibai_engine.storage.snapshots.keys import current_release_pointer_key
from baibai_engine.storage.snapshots.models import canonical_lake_model_bytes
from baibai_engine.storage.snapshots.objects import LakeObjectCache, open_lake
from baibai_engine.storage.snapshots.reader import resolve_current_release
from baibai_engine.storage.snapshots.release import LakeReleasePointer, create_release
from baibai_engine.storage.snapshots.sources import sha256_file
from baibai_engine.storage.snapshots.writer import export_dataset_snapshot, sealed_sqlite_snapshot
from baibai_engine.storage.sqlite.source_cache.lake_origin import (
    LakeStoreOrigin,
    read_lake_store_origin,
    write_lake_store_origin,
)
from baibai_jobs.publication import snapshot_write_cli
from baibai_jobs.publication.datasets import LAKE_DATASETS
from baibai_jobs.publication.policy import release_policy_for_profile
from baibai_jobs.publication.snapshot_cli import main
from baibai_web.materialize import export_read_models
from baibai_web.readmodel.builders import dashboard
from baibai_web.sources import db_sources, proposal

DISPLAY_DATASETS = (
    "jquants.daily_bars",
    "jquants.market_calendar",
    "jquants.master_snapshots",
)
ALL_DATASETS = (*DISPLAY_DATASETS, "jquants.short_sale_reports")
NOW = datetime.fromisoformat("2026-10-16T18:00:00+09:00")


@pytest.fixture
def portfolio_lake(tmp_path, monkeypatch, request):
    case = backend_case(tmp_path / "source", holding=True)
    TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED).publish(
        reviewed_proposal(case, action="hold")
    )
    PortfolioOutcomeStore(case.app).publish(_publication())
    missing = getattr(request, "param", "none")
    seed_daily_bars(
        case.market,
        [
            ("1234", "2026-10-15", 1100, 0.5 if missing == "share_basis" else 1.0),
            ("1234", "2026-10-16", None if missing == "quote" else 1100, 1.0),
        ],
    )
    with closing(sqlite3.connect(case.market)) as connection, connection:
        connection.execute(
            """INSERT INTO jquants_short_sale_reports(
                 disclosed_at, source_ordinal, calculated_at, ticker, short_seller_name,
                 discretionary_investment_contractor_name, investment_fund_name,
                 short_ratio, short_shares, short_trading_units
               ) VALUES ('2026-10-16', 0, '2026-10-15', '1234', 'Fixture', '', '', .01, 100, 1)"""
        )
        for source, count in (("jquants_daily_bars", 3), ("jquants_short_sale_reports", 1)):
            add_source_coverage(
                connection,
                source=source,
                coverage_key="synthetic-display",
                min_date="2026-10-14",
                max_date="2026-10-16",
                record_count=count,
            )
    narrow_release_policy(monkeypatch, datasets=ALL_DATASETS)
    mirror = tmp_path / "mirror"
    with sealed_sqlite_snapshot(sqlite_path=case.market, mirror_root=mirror) as snapshot:
        manifests = [
            export_dataset_snapshot(
                dataset=LAKE_DATASETS[name],
                mirror_root=mirror,
                source_snapshot=snapshot,
                producer_git_commit="a" * 40,
            ).manifest_path
            for name in ALL_DATASETS
        ]
    manifest, release = create_release(
        dataset_manifest_paths=manifests,
        mirror_root=mirror,
        release_id="portfolio-fixture",
        catalog=LAKE_DATASETS,
        policy=release_policy_for_profile("production"),
    )
    origin = LakeStoreOrigin(release.release_id, sha256_file(manifest))
    with closing(sqlite3.connect(case.market)) as connection, connection:
        write_lake_store_origin(connection, origin)
    pointer = LakeReleasePointer(
        release_id=origin.release_id,
        manifest_key=manifest.relative_to(mirror).as_posix(),
        manifest_sha256=origin.release_manifest_sha256,
    )
    current = mirror / current_release_pointer_key()
    current.parent.mkdir(parents=True, exist_ok=True)
    current.write_bytes(canonical_lake_model_bytes(pointer))
    with open_lake(mirror=mirror, bucket=None) as (_, cache):
        fixed = resolve_current_release(
            cache.source, catalog=LAKE_DATASETS, policy=release_policy_for_profile("production")
        )
    return case, mirror, origin, fixed, missing


def _compact(source: Path, root: Path, datasets=ALL_DATASETS) -> Path:
    path = root / MARKET_DB_PATH
    path.parent.mkdir(parents=True)
    shutil.copyfile(source, path)
    with closing(sqlite3.connect(path)) as connection, connection:
        for dataset in datasets:
            # Only known fixture catalog identifiers enter this statement.
            connection.execute(f"DELETE FROM {LAKE_DATASETS[dataset].sqlite_table}")
    return path


def _fill(command, mirror, path):
    return main([command, "--mirror", str(mirror), "--store", str(path)])


@pytest.mark.parametrize("portfolio_lake", ["none", "quote", "share_basis"], indirect=True)
def test_scoped_and_full_hydration_produce_identical_four_views(
    portfolio_lake, tmp_path, monkeypatch, capsys
):
    case, mirror, origin, _fixed, missing = portfolio_lake

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz is None else NOW.astimezone(tz)

    monkeypatch.setattr(db_sources, "datetime", Clock)
    monkeypatch.setattr(dashboard, "datetime", Clock)
    monkeypatch.setattr(proposal, "datetime", Clock)
    app_before = case.app.read_bytes()
    views = []
    for command in ("hydrate", "hydrate-portfolio"):
        root = tmp_path / command
        market = _compact(case.market, root)
        app = root / APPLICATION_DB_PATH
        app.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(case.app, app)
        assert _fill(command, mirror, market) == 0
        report = json.loads(capsys.readouterr().out)
        expected = ALL_DATASETS if command == "hydrate" else DISPLAY_DATASETS
        assert set(report["rows"]) == set(expected)
        assert read_lake_store_origin(market) == origin
        market_before = market.read_bytes()
        paths = export_read_models(root, root / "serving")
        views.append({path.name: path.read_bytes() for path in paths})
        assert len(paths) == 4
        assert app.read_bytes() == app_before
        assert market.read_bytes() == market_before
    assert views[0] == views[1]
    current = json.loads(views[1]["portfolio-dashboard.json"])
    assert current["holdings"][0]["company_name"] == "合成企業"
    assert current["holdings"][0]["quantity"] == 100
    assert current["holdings"][0]["market_value_yen"] == (110000 if missing == "none" else None)
    assert json.loads(views[1]["latest-proposal.json"])["status"] == "ready"
    assert json.loads(views[1]["outcomes.json"])["outcomes"][0]["portfolio_twr_pct"] == 10.2
    assert case.app.read_bytes() == app_before


def test_scoped_fill_skips_unrelated_bodies_and_keeps_native_cache(
    portfolio_lake, tmp_path, mocker, capsys
):
    case, mirror, _origin, fixed, _missing = portfolio_lake
    market = _compact(case.market, tmp_path / "scoped", DISPLAY_DATASETS)
    with closing(sqlite3.connect(market)) as connection, connection:
        connection.execute(
            "INSERT INTO edinet_list_responses VALUES (?, ?, ?, ?)",
            ("retained", "2026-10-14", "2026-10-14T00:00:00+00:00", b"retained-original"),
        )
        unrelated_before = connection.execute("SELECT * FROM jquants_short_sale_reports").fetchall()
        coverage_before = connection.execute("SELECT * FROM source_coverage").fetchall()
    unrelated = fixed.dataset_manifest("jquants.short_sale_reports")
    for partition in unrelated.partitions:
        for item in partition.objects:
            (mirror / item.key).unlink()  # unavailable only in this disposable fixture mirror
    cache_clear = mocker.patch.object(
        snapshot_write_cli, "_clear_independent_list_cache", side_effect=AssertionError("native")
    )
    materialized = []
    original = LakeObjectCache.materialize

    def record(self, item):
        materialized.append(item.key)
        return original(self, item)

    mocker.patch.object(LakeObjectCache, "materialize", record)
    assert _fill("hydrate-portfolio", mirror, market) == 0
    assert set(json.loads(capsys.readouterr().out)["rows"]) == set(DISPLAY_DATASETS)
    cache_clear.assert_not_called()
    assert set(materialized) == {
        item.key
        for name in DISPLAY_DATASETS
        for partition in fixed.dataset_manifest(name).partitions
        for item in partition.objects
    }
    with closing(sqlite3.connect(market)) as connection:
        assert connection.execute("SELECT body_gzip FROM edinet_list_responses").fetchone() == (
            b"retained-original",
        )
        assert (
            connection.execute("SELECT * FROM jquants_short_sale_reports").fetchall()
            == unrelated_before
        )
        assert connection.execute("SELECT * FROM source_coverage").fetchall() == coverage_before


@pytest.mark.parametrize("omitted", ["jquants.master_snapshots", "jquants.short_sale_reports"])
def test_scoped_fill_does_not_narrow_production_release_metadata_validation(
    portfolio_lake, tmp_path, mocker, capsys, omitted
):
    case, mirror, _origin, fixed, _missing = portfolio_lake
    market = _compact(case.market, tmp_path / "incomplete-release")
    entries = {name: value for name, value in fixed.manifest.datasets.items() if name != omitted}
    incomplete = fixed.manifest.model_copy(
        update={
            "datasets": entries,
            "data_as_of": min(item.data_as_of for item in entries.values()),
        }
    )
    manifest = mirror / fixed.manifest_key
    manifest.write_bytes(canonical_lake_model_bytes(incomplete))
    pointer = LakeReleasePointer(
        release_id=fixed.release_id,
        manifest_key=fixed.manifest_key,
        manifest_sha256=sha256_file(manifest),
    )
    (mirror / current_release_pointer_key()).write_bytes(canonical_lake_model_bytes(pointer))
    objects = mocker.patch.object(LakeObjectCache, "materialize")
    before = market.read_bytes()
    assert _fill("hydrate-portfolio", mirror, market) == 1
    assert "missing a required dataset" in capsys.readouterr().err
    objects.assert_not_called()
    assert market.read_bytes() == before


@pytest.mark.parametrize("failure", ["other_origin", "missing_origin", "corrupt_price", "moved"])
def test_scoped_fill_retains_release_integrity_and_leaves_failed_store_unchanged(
    portfolio_lake, tmp_path, mocker, capsys, failure
):
    case, mirror, origin, fixed, _missing = portfolio_lake
    market = _compact(case.market, tmp_path / "failed")
    if failure in {"other_origin", "missing_origin"}:
        with closing(sqlite3.connect(market)) as connection, connection:
            if failure == "missing_origin":
                connection.execute("DELETE FROM lake_store_origin")
            else:
                write_lake_store_origin(connection, LakeStoreOrigin("older", "b" * 64))
    elif failure == "corrupt_price":
        item = fixed.dataset_manifest("jquants.daily_bars").partitions[0].objects[0]
        (mirror / item.key).write_bytes(b"corrupt")
    else:
        calls = 0

        def moved_after_load():
            nonlocal calls
            calls += 1
            return (
                (origin.release_id, origin.release_manifest_sha256)
                if calls == 1
                else ("newer", "c" * 64)
            )

        mocker.patch.object(
            snapshot_write_cli, "_resolve_release_target", return_value=(fixed, moved_after_load)
        )
    before = market.read_bytes()
    assert _fill("hydrate-portfolio", mirror, market) == 1
    assert "error:" in capsys.readouterr().err
    assert market.read_bytes() == before
