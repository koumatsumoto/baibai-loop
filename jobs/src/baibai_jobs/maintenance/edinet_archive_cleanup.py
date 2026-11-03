"""One-time cleanup of a reviewed set of superseded EDINET archive objects.

Old releases referencing deleted objects no longer open unchanged. This does not
create a backup. Review and explicitly authorize each fixed plan before apply.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

from baibai_jobs.maintenance.source_input_cleanup import (
    _head,
    _journal,
    _require_absent,
    _unique_members,
    _verify_survivor_bytes,
)

BUCKET = "baibai-stores"
KEY = re.compile(
    r"lake/l1/source/edinet\.source_archives/contract=v1/year=2026/month=10/part-([0-9a-f]{64})\.parquet\Z"
)


def _read(client: Any, key: str) -> tuple[bytes, str]:
    obj = client.get_object(Bucket=BUCKET, Key=key)
    try:
        return obj["Body"].read(), obj["ETag"]
    finally:
        obj["Body"].close()


def _object(client: Any, item: dict[str, Any]) -> None:
    match = KEY.fullmatch(item["key"])
    if not match or match.group(1) != item["sha256"]:
        raise ValueError("object key and digest differ")
    head = _head(client, BUCKET, item["key"])
    if (
        head["ETag"] != item["etag"]
        or head["ContentLength"] != item["size"]
        or head.get("Metadata", {}).get("sha256") != item["sha256"]
    ):
        raise ValueError("object metadata changed")


def _current(client: Any, expected: dict[str, Any], keep: dict[str, Any]) -> None:
    payload, etag = _read(client, "lake/pointers/market/current.json")
    if etag != expected["etag"] or hashlib.sha256(payload).hexdigest() != expected["sha256"]:
        raise ValueError("market pointer changed")
    pointer = json.loads(payload, object_pairs_hook=_unique_members)
    release_payload, _ = _read(client, pointer["manifest_key"])
    if hashlib.sha256(release_payload).hexdigest() != pointer["manifest_sha256"]:
        raise ValueError("release manifest checksum mismatch")
    release = json.loads(release_payload, object_pairs_hook=_unique_members)
    dataset = release["datasets"]["edinet.source_archives"]
    path = f"lake/manifests/datasets/edinet.source_archives/{dataset['build_id']}.json"
    dataset_payload, _ = _read(client, path)
    if hashlib.sha256(dataset_payload).hexdigest() != dataset["manifest_sha256"]:
        raise ValueError("dataset manifest checksum mismatch")
    manifest = json.loads(dataset_payload, object_pairs_hook=_unique_members)
    objects = [o for p in manifest["partitions"] for o in p["objects"]]
    if not any(
        o["key"] == keep["key"] and o["sha256"] == keep["sha256"] and o["bytes"] == keep["size"]
        for o in objects
    ):
        raise ValueError("kept object is not in current release")


def _release_index(client: Any) -> dict[str, str]:
    found = {}
    for page in client.get_paginator("list_objects_v2").paginate(
        Bucket=BUCKET, Prefix="lake/manifests/releases/"
    ):
        for obj in page.get("Contents", []):
            if obj["Key"] in found:
                raise ValueError("duplicate release listing")
            found[obj["Key"]] = obj["ETag"]
    return found


def _active_pointers(client: Any, expected: dict[str, str], forbidden: set[str]) -> None:
    actual: dict[str, str] = {}
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET, Prefix="lake/pointers/"):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.endswith("/current.json"):
                continue
            raw, etag = _read(client, key)
            if key in actual or obj["ETag"] != etag:
                raise ValueError("pointer changed while enumerating")
            actual[key] = etag
            pointer = json.loads(raw, object_pairs_hook=_unique_members)
            release_raw, _ = _read(client, pointer["manifest_key"])
            if hashlib.sha256(release_raw).hexdigest() != pointer["manifest_sha256"]:
                raise ValueError("active release checksum mismatch")
            release = json.loads(release_raw, object_pairs_hook=_unique_members)
            dataset = release["datasets"].get("edinet.source_archives")
            if dataset is None:
                continue
            path = f"lake/manifests/datasets/edinet.source_archives/{dataset['build_id']}.json"
            data_raw, _ = _read(client, path)
            if hashlib.sha256(data_raw).hexdigest() != dataset["manifest_sha256"]:
                raise ValueError("active dataset checksum mismatch")
            data = json.loads(data_raw, object_pairs_hook=_unique_members)
            if any(o["key"] in forbidden for p in data["partitions"] for o in p["objects"]):
                raise ValueError("active pointer references a deletion target")
    if not actual or actual != expected:
        raise ValueError("active pointer inventory changed")


def _proof(path: Path, target: dict[str, Any], keep: dict[str, Any]) -> None:
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != target["proof_sha256"]:
        raise ValueError("proof changed")
    proof = json.loads(raw, object_pairs_hook=_unique_members)
    if (
        proof["old_key"] != target["key"]
        or proof["old_file_sha256"] != target["sha256"]
        or proof["current_key"] != keep["key"]
        or proof["current_file_sha256"] != keep["sha256"]
        or proof["old_HEAD_after"]["ETag"] != target["etag"]
        or proof["old_HEAD_after"]["bytes"] != target["size"]
        or proof["current_HEAD_after"]["ETag"] != keep["etag"]
        or proof["current_HEAD_after"]["bytes"] != keep["size"]
        or proof["old_is_exact_row_subset_of_current"] is not True
        or proof["all_fields_and_raw_bytes_equal"] is not True
        or proof["schema_and_metadata_equal"] is not True
        or proof["missing_in_current"] != 0
        or proof["changed_rows"] != 0
        or set(proof["comparison_fields"]) != {"doc_id", "source_type", "stored_on", "zip_content"}
    ):
        raise ValueError("proof does not establish exact row inclusion")


def run(
    client: Any,
    plan: dict[str, Any],
    proof_dir: Path,
    *,
    apply: bool = False,
    guard: Callable[[], None] | None = None,
    journal: TextIO | None = None,
    plan_sha256: str = "",
) -> dict[str, Any]:
    if apply and (guard is None or journal is None or not plan_sha256):
        raise ValueError("apply requires batch guard and durable journal")
    keep, targets = plan["keep"], plan["targets"]
    if (
        plan["bucket"] != BUCKET
        or len(targets) != 12
        or sum(t["size"] for t in targets) != 48_458_240_597
    ):
        raise ValueError("unexpected approved scope")
    keys = [t["key"] for t in targets]
    if len(set(keys)) != 12 or keep["key"] in keys:
        raise ValueError("duplicate or current object selected")
    _current(client, plan["pointer"], keep)
    _active_pointers(client, plan["active_pointers"], set(keys))
    if _release_index(client) != plan["release_index"]:
        raise ValueError("release inventory changed")
    _object(client, keep)
    for item in targets:
        if Path(item["proof"]).name != item["proof"]:
            raise ValueError("invalid proof path")
        _proof(proof_dir / item["proof"], item, keep)
        _object(client, item)
    _verify_survivor_bytes(client, BUCKET, keep)
    deleted: list[str] = []
    if apply:
        assert guard is not None
        assert journal is not None
        guard()
        for item in targets:
            _current(client, plan["pointer"], keep)
            _active_pointers(client, plan["active_pointers"], set(keys))
            if _release_index(client) != plan["release_index"]:
                raise ValueError("release inventory changed during cleanup")
            _object(client, keep)
            _object(client, item)
            _journal(journal, {"event": "intent", "plan_sha256": plan_sha256, "key": item["key"]})
            try:
                # Tested R2 ignores conditional DELETE; user accepted non-atomic risk.
                client.delete_object(Bucket=BUCKET, Key=item["key"])
                _require_absent(client, BUCKET, item["key"])
            except BaseException:
                _journal(journal, {"event": "outcome_unknown", "key": item["key"]})
                raise
            _journal(journal, {"event": "confirmed_absent", "key": item["key"]})
            deleted.append(item["key"])
        _current(client, plan["pointer"], keep)
        _object(client, keep)
        for item in targets:
            _require_absent(client, BUCKET, item["key"])
    return {
        "mode": "apply" if apply else "dry-run",
        "objects": len(targets),
        "bytes": sum(t["size"] for t in targets),
        "deleted": deleted,
    }


def main() -> None:
    import boto3
    from botocore.config import Config

    from baibai_engine.storage.objects.r2 import Boto3R2Store
    from baibai_jobs.maintenance.source_input_cleanup import _github_idle

    parser = argparse.ArgumentParser()
    parser.add_argument("plan", type=Path)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--journal", type=Path)
    args = parser.parse_args()
    payload = args.plan.read_bytes()
    if hashlib.sha256(payload).hexdigest() != args.plan_sha256:
        raise SystemExit("plan digest mismatch")
    plan = json.loads(payload, object_pairs_hook=_unique_members)
    store = Boto3R2Store(bucket=BUCKET)
    client = None
    try:
        if hashlib.sha256(store.endpoint.encode()).hexdigest() != plan["account_endpoint_sha256"]:
            raise ValueError("R2 account endpoint differs")
        if not args.apply:
            print(json.dumps(run(store._client(), plan, args.plan.parent)))
            return
        if args.journal is None:
            raise ValueError("apply requires a new journal")
        client = boto3.session.Session().client(
            "s3",
            endpoint_url=store.endpoint,
            region_name="auto",
            aws_access_key_id=store.access_key_id,
            aws_secret_access_key=store.secret_access_key,
            config=Config(
                connect_timeout=120,
                read_timeout=900,
                retries={"mode": "standard", "total_max_attempts": 1},
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )
        fd = os.open(args.journal, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        parent_fd = os.open(args.journal.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        with os.fdopen(fd, "w", encoding="utf-8") as journal:
            _journal(journal, {"event": "start", "plan_sha256": args.plan_sha256})
            result = run(
                client,
                plan,
                args.plan.parent,
                apply=True,
                guard=_github_idle,
                journal=journal,
                plan_sha256=args.plan_sha256,
            )
            _journal(journal, {"event": "completed", "objects": len(result["deleted"])})
            print(json.dumps(result))
    finally:
        if client is not None:
            client.close()
        store.close()


if __name__ == "__main__":
    main()
