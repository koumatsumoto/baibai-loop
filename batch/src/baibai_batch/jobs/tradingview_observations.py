"""Stage saved TV receipts and publish/read back them under the workflow's lease."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from baibai_batch.storage.lake_publish import Boto3R2Store, LakePublishError, ObjectStore
from baibai_batch.storage.publication_lease import PublicationLeaseHandle, _load_handle, verify_held
from baibai_batch.storage.publish_market_lake import (
    _fetch,
    _resolve_serving_release,
    _serving_pointer,
    publish_market_lake,
)
from baibai_engine.batch_api import (
    MARKET_DB_PATH,
    LakeFixedRelease,
    LakeObjectCache,
    LocalMirrorSource,
    hydrate_market_store,
    lake_session,
    open_market_store,
    read_lake_store_origin,
    tradingview_accept_observations,
    tradingview_load_observations,
    tradingview_verify_observations,
)

DATASET = "tradingview.forecast_snapshots"


def stage(event: Path, directory: Path) -> None:
    """Never interpolate or echo raw workflow inputs into shell scripts or env."""
    payload = json.loads(event.read_bytes())
    inputs = payload["inputs"]
    raw = inputs["observations_json"]
    digest = inputs["observations_sha256"]
    if not isinstance(raw, str) or not isinstance(digest, str):
        raise ValueError("invalid saved-observation inputs")
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    path = directory / "observations.json"
    path.write_text(raw, encoding="utf-8")
    path.chmod(0o600)
    accepted = tradingview_load_observations(path, sha256=digest)
    (directory / "input.sha256").write_text(digest, encoding="ascii")
    print(
        json.dumps(
            {
                "input_sha256": accepted.sha256,
                "source_ref": accepted.document.source_ref.model_dump(),
                "rows": len(accepted.values),
            },
            sort_keys=True,
        )
    )


def _unchanged_objects(release: LakeFixedRelease, month: tuple[int, int]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, manifest in release.dataset_manifests.items():
        objects = [
            obj.model_dump()
            for part in manifest.partitions
            if name != DATASET or (part.values["year"], part.values["month"]) != month
            for obj in part.objects
        ]
        if name != DATASET or objects:
            result[name] = objects
    return result


def publish_and_verify(
    directory: Path, *, sqlite_path: Path, mirror: Path, store: ObjectStore, handle: PublicationLeaseHandle
) -> dict[str, object]:
    """Keep the existing publication lease through pointer switch and readback."""
    verify_held(store, handle)
    accepted = tradingview_load_observations(
        directory / "observations.json", sha256=(directory / "input.sha256").read_text()
    )
    before_pointer = _serving_pointer(store).pointer
    if before_pointer is None:
        raise LakePublishError("saved observations require an existing L1 release")
    before = _resolve_serving_release(store, mirror, before_pointer)
    origin = read_lake_store_origin(sqlite_path)
    if origin is None or (origin.release_id, origin.release_manifest_sha256) != (
        before.release_id,
        before.manifest_sha256,
    ):
        raise LakePublishError("saved observations require a fresh fixed-release hydrate")
    acceptance = tradingview_accept_observations(sqlite_path, accepted)
    (directory / "acceptance.json").write_text(json.dumps(acceptance, sort_keys=True))
    publication = None
    if acceptance["rows_added"]:
        publication = publish_market_lake(
            sqlite_path=sqlite_path, mirror_root=mirror, store=store,
            before_pointer_switch=lambda: verify_held(store, handle),
        )
        print(
            json.dumps(
                {"status": "published_readback_pending", **publication.as_dict()}, sort_keys=True
            )
        )
    current = _serving_pointer(store).pointer
    if current is None:
        raise LakePublishError("saved-observation release is unavailable")
    if publication is not None and (current.release_id, current.manifest_sha256) != (
        publication.release_id,
        publication.release_manifest_sha256,
    ):
        raise LakePublishError("saved-observation publication changed before readback")
    # Independently read back all TradingView history. Unrelated dataset objects
    # are already compared against the pinned previous graph, not downloaded again.
    readback_mirror = directory / "readback-mirror"
    readback_mirror.mkdir()
    after = _resolve_serving_release(store, readback_mirror, current)
    month = (
        accepted.document.observation_date_jst.year,
        accepted.document.observation_date_jst.month,
    )
    if set(after.dataset_names()) != set(before.dataset_names()) | {DATASET}:
        raise LakePublishError("saved-observation publication changed the dataset inventory")
    if _unchanged_objects(before, month) != _unchanged_objects(after, month):
        raise LakePublishError("saved-observation publication changed unrelated data")
    tv = after.dataset_manifest(DATASET)
    if tv.coverage_status != "partial":
        raise LakePublishError("saved-observation publication claimed complete coverage")
    for part in tv.partitions:
        for obj in part.objects:
            _fetch(store, readback_mirror, obj.key, expected_sha256=obj.sha256)
    readback_store = directory / "readback.sqlite"
    open_market_store(readback_store).close()
    with lake_session() as session:
        hydrate_market_store(
            session,
            release=after,
            cache=LakeObjectCache(root=readback_mirror, source=LocalMirrorSource(readback_mirror)),
            store=readback_store,
            dataset_names=(DATASET,),
        )
    tradingview_verify_observations(readback_store, accepted, acceptance)
    verify_held(store, handle)
    report = {
        **acceptance,
        "release_id": after.release_id,
        "release_manifest_sha256": after.manifest_sha256,
        "previous_release_id": before.release_id,
        "unchanged_other_datasets": len(set(before.dataset_names()) - {DATASET}),
        "readback": "verified",
    }
    (directory / "readback-report.json").write_text(json.dumps(report, sort_keys=True))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("stage", "publish"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--event", type=Path)
    parser.add_argument("--lease-handle", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "stage":
            if args.event is None:
                raise ValueError("event required")
            stage(args.event, args.directory)
        else:
            if args.lease_handle is None:
                raise ValueError("publication lease handle required")
            report = publish_and_verify(
                args.directory,
                sqlite_path=MARKET_DB_PATH,
                handle=_load_handle(args.lease_handle),
                mirror=MARKET_DB_PATH.parents[1],
                store=Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores"),
            )
            print(json.dumps(report, sort_keys=True))
            if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
                with Path(summary).open("a", encoding="utf-8") as stream:
                    stream.write("保存済みTradingView実観測の受入・固定release読戻し\n\n")
                    stream.write("```json\n" + json.dumps(report, sort_keys=True) + "\n```\n")
        return 0
    except Exception as exc:
        print(f"Saved TradingView acceptance failed; type={type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
