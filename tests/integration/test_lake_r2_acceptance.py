from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
from pathlib import Path

import pytest
from tests.helpers.calibration_store import publish_panel

from baibai_batch.storage.lake_publish import (
    AwsCliR2Store,
    LakeCASConflict,
    publish_calibration_bundle,
    rollback_calibration_bundle,
)
from baibai_engine.market.lake.keys import current_calibration_bundle_pointer_key
from baibai_engine.market.lake.models import canonical_lake_model_bytes
from baibai_engine.screening.calibration.lake import CalibrationBundlePointer

pytestmark = pytest.mark.skipif(
    os.environ.get("BAIBAI_R2_ACCEPTANCE") != "1",
    reason="dedicated R2 acceptance credentials are not configured",
)


def _pointer(root: Path) -> CalibrationBundlePointer:
    return CalibrationBundlePointer.model_validate_json(
        (root / current_calibration_bundle_pointer_key()).read_bytes()
    )


def test_actual_r2_bundle_cas_and_rollback_identity(tmp_path: Path) -> None:
    bucket = os.environ["R2_LAKE_ACCEPTANCE_BUCKET"]
    if bucket == "baibai-stores" or "acceptance" not in bucket:
        pytest.fail("R2 acceptance must use a dedicated non-production bucket")
    store = AwsCliR2Store(bucket=bucket)
    mirror = tmp_path / "mirror"

    publish_panel(mirror, "2026-01-30", [])
    first = _pointer(mirror)
    existing = store.head(current_calibration_bundle_pointer_key())
    if existing is not None:
        existing_pointer = CalibrationBundlePointer.model_validate_json(
            store.get_bytes(current_calibration_bundle_pointer_key())
        )
        first = CalibrationBundlePointer(
            current=first.current,
            previous=existing_pointer.current,
        )
        (mirror / current_calibration_bundle_pointer_key()).write_bytes(
            canonical_lake_model_bytes(first)
        )
    first_report = publish_calibration_bundle(
        mirror_root=mirror,
        bundle_manifest_path=mirror / first.current.manifest_key,
        store=store,
    )
    first_remote = store.head(current_calibration_bundle_pointer_key())
    assert first_remote is not None

    publish_panel(mirror, "2026-02-27", [])
    second = _pointer(mirror)
    second_report = publish_calibration_bundle(
        mirror_root=mirror,
        bundle_manifest_path=mirror / second.current.manifest_key,
        store=store,
    )
    remote_pointer = CalibrationBundlePointer.model_validate_json(
        store.get_bytes(current_calibration_bundle_pointer_key())
    )
    assert remote_pointer.current.bundle_id == second_report.bundle_id
    assert remote_pointer.previous is not None
    assert remote_pointer.previous.bundle_id == first_report.bundle_id

    rollback_report = rollback_calibration_bundle(store=store)
    rolled_back = CalibrationBundlePointer.model_validate_json(
        store.get_bytes(current_calibration_bundle_pointer_key())
    )
    assert rollback_report.bundle_id == first_report.bundle_id
    assert rolled_back.current == first.current
    assert rolled_back.previous == second.current
    first_bundle = store.get_bytes(rolled_back.current.manifest_key)
    assert hashlib.sha256(first_bundle).hexdigest() == rolled_back.current.manifest_sha256

    stale_payload = (
        json.dumps(
            rolled_back.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
        + b"\n"
    )
    with tempfile.NamedTemporaryFile() as temporary:
        Path(temporary.name).write_bytes(stale_payload)
        with pytest.raises(LakeCASConflict):
            store.put_file(
                current_calibration_bundle_pointer_key(),
                Path(temporary.name),
                sha256=hashlib.sha256(stale_payload).hexdigest(),
                content_md5=base64.b64encode(
                    hashlib.md5(stale_payload, usedforsecurity=False).digest()  # nosec B324
                ).decode(),
                content_type="application/json",
                if_match=first_remote.etag,
            )
    assert (
        CalibrationBundlePointer.model_validate_json(
            store.get_bytes(current_calibration_bundle_pointer_key())
        ).current.bundle_id
        == first_report.bundle_id
    )
