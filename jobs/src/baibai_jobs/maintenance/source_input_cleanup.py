"""One-time, manifest-bound cleanup of duplicate immutable source-input receipts.

Never operates on lake, handoff manifests, pointers, or current inputs. A saved
byte-identical receipt is required for each deletion. Retaining bytes alone does
not preserve the old handoff path: historical replay of deleted paths may fail.
The operator must separately verify there are no in-flight readers before apply.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any, cast

PREFIX = "coordination/source-inputs/"
KEY = re.compile(
    r"coordination/source-inputs/([A-Za-z0-9][A-Za-z0-9_-]{0,119})/receipts-([0-9a-f]{64})\.sqlite\Z"
)


def _head(client: Any, bucket: str, key: str) -> dict[str, Any]:
    return cast("dict[str, Any]", client.head_object(Bucket=bucket, Key=key))


def _check_receipt(client: Any, bucket: str, item: dict[str, Any]) -> None:
    match = KEY.fullmatch(item["key"])
    if not match or match.group(2) != item["sha256"]:
        raise ValueError("receipt key and digest mismatch")
    h = _head(client, bucket, item["key"])
    if (
        h["ETag"] != item["etag"]
        or h["ContentLength"] != item["size"]
        or h.get("Metadata", {}).get("sha256") != item["sha256"]
    ):
        raise ValueError("receipt metadata changed")


def _verify_survivor_bytes(client: Any, bucket: str, item: dict[str, Any]) -> None:
    response = client.get_object(Bucket=bucket, Key=item["key"], IfMatch=item["etag"])
    digest = hashlib.sha256()
    size = 0
    body = response["Body"]
    try:
        while chunk := body.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    finally:
        body.close()
    if size != item["size"] or digest.hexdigest() != item["sha256"]:
        raise ValueError("survivor bytes do not match receipt digest")


def _pointers(client: Any, bucket: str) -> dict[str, tuple[str, str]]:
    result: dict[str, tuple[str, str]] = {}
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=PREFIX):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.startswith(PREFIX) and key.endswith(".json") and "/" not in key[len(PREFIX) :]:
                response = client.get_object(Bucket=bucket, Key=key)
                body = response["Body"]
                try:
                    obj_data = json.loads(body.read())
                finally:
                    body.close()
                if not isinstance(obj_data, dict) or not isinstance(obj_data.get("stage_id"), str):
                    raise ValueError("invalid current pointer")
                result[key] = (response["ETag"], obj_data["stage_id"])
    if not result:
        raise ValueError("no current pointers discovered")
    return result


def run(client: Any, plan: dict[str, Any], *, apply: bool = False) -> dict[str, Any]:
    """Validate every precondition before deletion; abort on the first error."""
    if apply:
        raise RuntimeError("apply disabled pending reference, concurrency and R2 safety review")
    if set(plan) != {"bucket", "pointers", "groups"} or plan["bucket"] != "baibai-stores":
        raise ValueError("unexpected cleanup scope")
    bucket = plan["bucket"]
    expected = {k: tuple(v) for k, v in plan["pointers"].items()}
    if _pointers(client, bucket) != expected:
        raise ValueError("current pointers changed or enumeration incomplete")
    current_stages = {stage for _, stage in expected.values()}
    all_keys: set[str] = set()
    deletions: list[dict[str, Any]] = []
    for group in plan["groups"]:
        if set(group) != {"keep", "delete"} or not group["delete"]:
            raise ValueError("invalid duplicate group")
        keep = group["keep"]
        for item in [keep, *group["delete"]]:
            if set(item) != {"key", "sha256", "size", "etag"} or item["key"] in all_keys:
                raise ValueError("invalid or repeated object")
            all_keys.add(item["key"])
            match = KEY.fullmatch(item["key"])
            if not match or match.group(1) in current_stages:
                raise ValueError("current or invalid source input")
            if (
                item["sha256"] != keep["sha256"]
                or item["size"] != keep["size"]
                or item["etag"] != keep["etag"]
            ):
                raise ValueError("duplicate identity differs")
            _check_receipt(client, bucket, item)
        deletions.extend(group["delete"])
    if not deletions:
        raise ValueError("empty deletion plan")
    for group in plan["groups"]:
        _verify_survivor_bytes(client, bucket, group["keep"])
    report: dict[str, Any] = {
        "mode": "apply" if apply else "dry-run",
        "objects": len(deletions),
        "bytes": sum(x["size"] for x in deletions),
        "deleted": [],
    }
    if not apply:
        return report
    # Recheck pointer and survivor before every object. If a failure occurs,
    # stop and retain the successful keys in the operator's local audit log.
    for group in plan["groups"]:
        for item in group["delete"]:
            if _pointers(client, bucket) != expected:
                raise ValueError("current pointers changed during apply")
            _check_receipt(client, bucket, group["keep"])
            _check_receipt(client, bucket, item)
            client.delete_object(Bucket=bucket, Key=item["key"], IfMatch=item["etag"])
            report["deleted"].append(item["key"])
            print(json.dumps({"deleted": item["key"], "etag": item["etag"]}), flush=True)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("plan", type=Path)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    payload = args.plan.read_bytes()
    if hashlib.sha256(payload).hexdigest() != args.plan_sha256:
        raise SystemExit("plan digest mismatch")
    from baibai_engine.storage.objects.r2 import Boto3R2Store

    store = Boto3R2Store(bucket="baibai-stores")
    try:
        print(
            json.dumps(
                run(store._client(), json.loads(payload), apply=args.apply), ensure_ascii=False
            )
        )
    finally:
        store.close()


if __name__ == "__main__":
    main()
