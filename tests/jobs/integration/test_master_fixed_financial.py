"""Fixed local financial reads honor exact unavailable master boundaries."""

import json
from datetime import timedelta

import pytest
from tests.helpers.lake_policy import narrow_release_policy
from tests.jobs.integration.test_master_native_holidays import FRIDAY, SUNDAY, calendar
from tests.jobs.integration.test_master_native_models import (
    AT,
    DAY,
    NEXT,
    attempt,
    records,
    setup_native,
)

from baibai_engine.machine_analysis.observations.financials.fixed_financial_reader import (
    read_fixed_financial_documents,
)
from baibai_engine.machine_analysis.observations.jquants_writer import store_jquants_fin_summaries
from baibai_engine.storage.snapshots.models import (
    DatasetManifest,
    canonical_lake_model_bytes,
    load_lake_model_json,
)
from baibai_engine.storage.snapshots.release import create_release
from baibai_engine.storage.snapshots.sources import sha256_file
from baibai_engine.storage.snapshots.writer import export_dataset_snapshot, sealed_sqlite_snapshot
from baibai_engine.storage.sqlite.source_cache.lake_origin import LakeStoreOrigin
from baibai_jobs.integration.master_native import model_input
from baibai_jobs.publication.datasets import LAKE_DATASETS
from baibai_jobs.publication.l1_master_export import export_master_from_native_input
from baibai_jobs.publication.policy import release_policy_for_profile


def fixed_release(tmp_path, monkeypatch, market, saved, *, legacy=False, with_calendar=False):
    names = ("jquants.fin_summaries", "jquants.master_snapshots") + (
        ("jquants.market_calendar",) if with_calendar else ()
    )
    narrow_release_policy(monkeypatch, datasets=names)
    store_jquants_fin_summaries(
        market,
        [{"Code": "13010", "DiscDate": DAY.isoformat(), "DiscNo": "fixed"}],
        requested_start=DAY,
        requested_end=DAY,
    )
    mirror = tmp_path / "mirror"
    with sealed_sqlite_snapshot(sqlite_path=market, mirror_root=mirror) as snapshot:
        reports = [
            export_dataset_snapshot(
                dataset=LAKE_DATASETS[name],
                source_snapshot=snapshot,
                mirror_root=mirror,
                producer_git_commit="a" * 40,
            )
            for name in names
            if name != "jquants.master_snapshots"
        ]
        master = (
            export_dataset_snapshot(
                dataset=LAKE_DATASETS["jquants.master_snapshots"],
                source_snapshot=snapshot,
                mirror_root=mirror,
                producer_git_commit="a" * 40,
            )
            if legacy
            else export_master_from_native_input(
                source_snapshot=snapshot,
                saved=saved,
                mirror_root=mirror,
                producer_git_commit="a" * 40,
            )
        )
    path, release = create_release(
        dataset_manifest_paths=[
            master.manifest_path,
            *(report.manifest_path for report in reports),
        ],
        mirror_root=mirror,
        release_id="fixed",
        catalog=LAKE_DATASETS,
        policy=release_policy_for_profile("production"),
    )
    return (
        mirror,
        LakeStoreOrigin(release_id=release.release_id, release_manifest_sha256=sha256_file(path)),
        master.manifest,
    )


@pytest.mark.parametrize("invalid_count", [1, 16])
def test_all_invalid_boundaries_survive_local_fixed_read_and_preserve_recovered_prior(
    tmp_path, monkeypatch, invalid_count
):
    invalid_days = tuple(DAY + timedelta(days=i) for i in range(invalid_count))
    store, owner, market = setup_native(
        tmp_path,
        [
            attempt("prior", day=DAY - timedelta(days=1)),
            *(attempt(f"invalid-{day}", day=day, rows=[]) for day in invalid_days),
            attempt("recovered", day=NEXT),
        ],
    )
    saved = model_input(store, owner.fixed(), as_of=NEXT, evaluated_at=AT, market=market)
    assert len(saved.descriptor["summary"]["invalid_snapshot_dates"]) == min(invalid_count, 12)
    mirror, origin, manifest = fixed_release(tmp_path, monkeypatch, market, saved)
    assert manifest.unavailable_dates == invalid_days
    assert (
        load_lake_model_json(
            canonical_lake_model_bytes(manifest), DatasetManifest
        ).unavailable_dates
        == invalid_days
    )
    market.unlink()
    store.values.clear()  # The fixed reader has only local release objects, no L1 metadata/head.
    for day in invalid_days:
        assert (
            read_fixed_financial_documents(mirror, origin=origin, ticker="1301", as_of=day).sector
            is None
        )
    for day in (DAY - timedelta(days=1), NEXT, NEXT + timedelta(days=1)):
        assert (
            read_fixed_financial_documents(mirror, origin=origin, ticker="1301", as_of=day).sector
            == "卸売業"
        )


def test_legacy_partial_manifest_keeps_prior_behavior(tmp_path, monkeypatch):
    store, owner, market = setup_native(
        tmp_path,
        [
            attempt("prior", day=DAY - timedelta(days=1)),
            attempt("invalid", rows=[]),
            attempt("recovered", day=NEXT),
        ],
    )
    saved = model_input(store, owner.fixed(), as_of=NEXT, evaluated_at=AT, market=market)
    mirror, origin, manifest = fixed_release(tmp_path, monkeypatch, market, saved, legacy=True)
    assert manifest.unavailable_dates == ()
    assert "unavailable_dates" not in json.loads(canonical_lake_model_bytes(manifest))
    assert (
        read_fixed_financial_documents(mirror, origin=origin, ticker="1301", as_of=DAY).sector
        == "卸売業"
    )


@pytest.mark.parametrize("response", ["empty", "prior-date", "matching-date"])
def test_fixed_native_reader_uses_confirmed_local_holiday_basis(tmp_path, monkeypatch, response):
    raw = [] if response == "empty" else records(FRIDAY if response == "prior-date" else SUNDAY)
    for row in raw:
        row["S33Nm"] = "銀行業"
    store, owner, market = setup_native(
        tmp_path, [attempt("business", day=FRIDAY), attempt("holiday", day=SUNDAY, rows=raw)]
    )
    calendar(market)
    saved = model_input(store, owner.fixed(), as_of=SUNDAY, evaluated_at=AT, market=market)
    mirror, origin, manifest = fixed_release(
        tmp_path, monkeypatch, market, saved, with_calendar=True
    )
    assert manifest.unavailable_dates == ()  # Known closed invalid requests are not barriers.
    market.unlink()
    store.values.clear()
    assert (
        read_fixed_financial_documents(mirror, origin=origin, ticker="1301", as_of=SUNDAY).sector
        == "卸売業"
    )


@pytest.mark.parametrize("name", ["jquants.daily_bars", "jquants.fin_summaries"])
def test_empty_unavailable_dates_leave_existing_manifest_bytes_unchanged(name):
    from tests.engine.storage.test_market_lake import _dataset_payload

    payload = _dataset_payload(dataset=name)
    manifest = load_lake_model_json(json.dumps(payload).encode(), DatasetManifest)
    previous_shape = manifest.model_dump(mode="json", exclude={"unavailable_dates"})
    expected = (
        json.dumps(
            previous_shape, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        + b"\n"
    )
    assert canonical_lake_model_bytes(manifest) == expected
    assert "unavailable_dates" not in manifest.model_dump(mode="json")
