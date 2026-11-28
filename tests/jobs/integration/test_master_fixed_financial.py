"""An absent unavailable-date list is omitted from the manifest wire shape."""

import json

import pytest

from baibai_engine.storage.snapshots.models import (
    DatasetManifest,
    canonical_lake_model_bytes,
    load_lake_model_json,
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
