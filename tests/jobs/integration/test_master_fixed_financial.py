"""Manifest keeps all unavailable dates, independently of the bounded execution summary."""

import json
from datetime import timedelta

import pytest
from tests.jobs.integration.test_master_native_models import (
    AT,
    DAY,
    NEXT,
    attempt,
    setup_native,
)

from baibai_engine.storage.snapshots.models import (
    DatasetManifest,
    canonical_lake_model_bytes,
    load_lake_model_json,
)
from baibai_engine.storage.snapshots.writer import sealed_sqlite_snapshot
from baibai_jobs.integration.master_native import model_input
from baibai_jobs.publication.l1_master_export import export_master_from_native_input


@pytest.mark.parametrize("invalid_count", [1, 16])
def test_native_master_manifest_retains_every_unavailable_date(tmp_path, invalid_count):
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
    mirror = tmp_path / "mirror"
    with sealed_sqlite_snapshot(sqlite_path=market, mirror_root=mirror) as seal:
        manifest = export_master_from_native_input(
            source_snapshot=seal, saved=saved, mirror_root=mirror, producer_git_commit="a" * 40
        ).manifest
    assert manifest.unavailable_dates == invalid_days
    assert (
        load_lake_model_json(
            canonical_lake_model_bytes(manifest), DatasetManifest
        ).unavailable_dates
        == invalid_days
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
