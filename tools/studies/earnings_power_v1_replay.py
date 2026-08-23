"""Execute the pre-registered Earnings Power v1 replay against its fixed bundle once."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

from baibai_engine.foundation.filesystem import write_text_atomic
from baibai_engine.screening.calibration.lake import (
    CALIBRATION_DIAGNOSTICS,
    CALIBRATION_FORWARD,
    CALIBRATION_PANEL,
    L2Dataset,
    asof_month,
    read_l2_partition,
)
from baibai_engine.screening.calibration.store import (
    DEFAULT_CALIBRATION_DIR,
    FixedCalibrationBundle,
    forward_row_from_mapping,
    panel_row_from_mapping,
    published_cohorts,
    resolve_calibration_bundle,
)
from baibai_engine.screening.selection.earnings_replay import evaluate_frozen_replay

_BUNDLE_ID = "20260820T073844Z-02061626ff91452cb645ef3c0ffc54a1"
_BUNDLE_MANIFEST_SHA256 = "e189c36dc0f098747be30569d8f934612b45a51bddf71ed47964758e47042cb4"
_OUTPUT = Path("reports/studies/2026-08-24-earnings-power-v1/historical-replay.yaml")


def _pinned_payloads(
    root: Path,
    bundle: FixedCalibrationBundle,
    dataset: L2Dataset,
    asof: str,
) -> list[dict[str, object]]:
    """Read one fixed historical cohort without imposing the current transform hash.

    ``read_l2_partition`` still checks the dataset contract, every immutable object
    SHA/size, row count, ordering, primary-key uniqueness, and partition boundary. The
    omitted check is only equality with today's source transform: the preregistration
    intentionally pins the old transform and bridges the terminology-only rename by a
    separate semantic-equivalence report.
    """
    payloads = [
        payload
        for payload in read_l2_partition(
            dataset=dataset,
            manifest=bundle.datasets[dataset.name],
            mirror_root=root,
            month=asof_month(asof),
        )
        if str(payload["asof"]) == asof
    ]
    inventory = bundle.cohorts[asof]
    expected = {
        CALIBRATION_PANEL.name: inventory.panel.rows,
        CALIBRATION_DIAGNOSTICS.name: inventory.diagnostics.rows,
        CALIBRATION_FORWARD.name: inventory.forward.rows,
    }[dataset.name]
    if len(payloads) != expected:
        raise RuntimeError(f"{dataset.name}: fixed cohort row count differs from inventory")
    return payloads


def main() -> int:
    if _OUTPUT.exists():
        print(f"refusing to repeat frozen replay: {_OUTPUT} already exists", file=sys.stderr)
        return 1
    root = DEFAULT_CALIBRATION_DIR
    bundle = resolve_calibration_bundle(root)
    if bundle.ref.bundle_id != _BUNDLE_ID or bundle.ref.manifest_sha256 != _BUNDLE_MANIFEST_SHA256:
        print("current calibration bundle differs from the preregistered identity", file=sys.stderr)
        return 1
    asofs = [
        asof
        for asof in published_cohorts(root, bundle=bundle)
        if bundle.cohorts[asof.isoformat()].forward.status == "complete"
    ]
    panels = {
        asof.isoformat(): [
            panel_row_from_mapping(payload)
            for payload in _pinned_payloads(root, bundle, CALIBRATION_PANEL, asof.isoformat())
        ]
        for asof in asofs
    }
    metas = {
        asof.isoformat(): _pinned_payloads(root, bundle, CALIBRATION_DIAGNOSTICS, asof.isoformat())[
            0
        ]
        for asof in asofs
    }
    # This is the only outcome read.  The output guard and fixed bundle identity make
    # a completed study an immutable observation rather than a threshold-tuning loop.
    forwards = {
        asof.isoformat(): [
            forward_row_from_mapping(payload)
            for payload in _pinned_payloads(root, bundle, CALIBRATION_FORWARD, asof.isoformat())
        ]
        for asof in asofs
    }
    payload = evaluate_frozen_replay(
        panels,
        forwards,
        metas,
        bundle_id=bundle.ref.bundle_id,
        bundle_manifest_sha256=bundle.ref.manifest_sha256,
    )
    write_text_atomic(
        _OUTPUT,
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
    )
    print(f"wrote {_OUTPUT}: verdict={payload['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
