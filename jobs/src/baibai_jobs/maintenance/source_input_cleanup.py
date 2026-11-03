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
import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO, cast

from botocore.exceptions import ClientError

PREFIX = "coordination/source-inputs/"
KEY = re.compile(
    r"coordination/source-inputs/([A-Za-z0-9][A-Za-z0-9_-]{0,119})/receipts-([0-9a-f]{64})\.sqlite\Z"
)


def _unique_members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


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


def _handoff_receipts(client: Any, bucket: str) -> dict[str, tuple[str, str]]:
    """Map each exact receipt to its stage manifest and the manifest's digest."""
    referenced: dict[str, tuple[str, str]] = {}
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=PREFIX):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.startswith(PREFIX) or not key.endswith("/handoff.json"):
                continue
            relative = key[len(PREFIX) :]
            if relative.count("/") != 1:
                raise ValueError("unexpected handoff path")
            response = client.get_object(Bucket=bucket, Key=key)
            body = response["Body"]
            try:
                payload = body.read()
                handoff = json.loads(payload, object_pairs_hook=_unique_members)
            finally:
                body.close()
            if not isinstance(handoff, dict) or not isinstance(handoff.get("receipt_key"), str):
                raise ValueError("invalid handoff")
            receipt = handoff["receipt_key"]
            if not KEY.fullmatch(receipt) or not receipt.startswith(
                key.removesuffix("handoff.json")
            ):
                raise ValueError("handoff references an unexpected receipt")
            if receipt in referenced:
                raise ValueError("multiple handoffs reference a receipt")
            referenced[receipt] = (key, hashlib.sha256(payload).hexdigest())
    return referenced


def _check_retired_handoff(
    client: Any, bucket: str, receipt: str, expected: tuple[str, str] | None
) -> None:
    if expected is None:
        raise ValueError("a deleted receipt must have an explicitly retired handoff")
    key, digest = expected
    if key != receipt.rsplit("/", 1)[0] + "/handoff.json":
        raise ValueError("retired handoff path differs")
    response = client.get_object(Bucket=bucket, Key=key)
    body = response["Body"]
    try:
        payload = body.read()
    finally:
        body.close()
    if hashlib.sha256(payload).hexdigest() != digest:
        raise ValueError("retired handoff changed")
    handoff = json.loads(payload, object_pairs_hook=_unique_members)
    if not isinstance(handoff, dict) or handoff.get("receipt_key") != receipt:
        raise ValueError("retired handoff receipt changed")


def _journal(stream: TextIO, event: dict[str, Any]) -> None:
    stream.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    stream.flush()
    os.fsync(stream.fileno())


def _github_idle() -> None:
    """Refuse cleanup if a source reader or publisher is active or cannot be checked."""
    for workflow in ("cloud-market-sources.yml", "cloud-daily-batch.yml"):
        for status in ("queued", "in_progress", "waiting", "requested", "pending"):
            endpoint = (
                f"repos/koumatsumoto/baibai-loop/actions/workflows/{workflow}/runs"
                f"?status={status}&per_page=1"
            )
            result = subprocess.run(
                ["gh", "api", endpoint], capture_output=True, text=True, check=False, timeout=30
            )
            if result.returncode != 0:
                raise RuntimeError("cannot verify GitHub Actions activity")
            response = json.loads(result.stdout, object_pairs_hook=_unique_members)
            if type(response.get("total_count")) is not int or response["total_count"] != 0:
                raise RuntimeError("source reader or publisher is active")


def run(
    client: Any,
    plan: dict[str, Any],
    *,
    apply: bool = False,
    guard: Callable[[], None] | None = None,
    journal: TextIO | None = None,
    plan_sha256: str | None = None,
) -> dict[str, Any]:
    """Validate every precondition before deletion; abort on the first error."""
    if apply and (guard is None or journal is None or plan_sha256 is None):
        raise RuntimeError("apply requires verified lease, run guard and durable journal")
    if (
        set(plan) != {"bucket", "account_endpoint_sha256", "pointers", "groups", "retired_handoffs"}
        or plan["bucket"] != "baibai-stores"
    ):
        raise ValueError("unexpected cleanup scope")
    bucket = plan["bucket"]
    expected = {k: tuple(v) for k, v in plan["pointers"].items()}
    if _pointers(client, bucket) != expected:
        raise ValueError("current pointers changed or enumeration incomplete")
    current_stages = {stage for _, stage in expected.values()}
    referenced = _handoff_receipts(client, bucket)
    retired = {key: tuple(value) for key, value in plan["retired_handoffs"].items()}
    all_keys: set[str] = set()
    deletions: list[dict[str, Any]] = []
    for group in plan["groups"]:
        if set(group) != {"keep", "delete"} or not group["delete"]:
            raise ValueError("invalid duplicate group")
        keep = group["keep"]
        for item in [keep, *group["delete"]]:
            if (
                not isinstance(item, dict)
                or set(item) != {"key", "sha256", "size", "etag"}
                or not isinstance(item["key"], str)
                or not isinstance(item["sha256"], str)
                or type(item["size"]) is not int
                or item["size"] <= 0
                or not isinstance(item["etag"], str)
                or not item["etag"]
                or item["key"] in all_keys
            ):
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
        for item in group["delete"]:
            actual = referenced.get(item["key"])
            allowed = retired.get(item["key"])
            if actual != allowed:
                raise ValueError("handoff reference differs from approved plan")
        deletions.extend(group["delete"])
    if not deletions:
        raise ValueError("empty deletion plan")
    if set(retired) != {item["key"] for item in deletions if item["key"] in referenced}:
        raise ValueError("retired handoff scope differs from approved plan")
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
    assert guard is not None
    assert journal is not None
    assert plan_sha256 is not None
    for group in plan["groups"]:
        for item in group["delete"]:
            guard()
            if _pointers(client, bucket) != expected:
                raise ValueError("current pointers changed during cleanup")
            _check_retired_handoff(client, bucket, item["key"], retired.get(item["key"]))
            _check_receipt(client, bucket, group["keep"])
            _check_receipt(client, bucket, item)
            _journal(journal, {"event": "intent", "plan_sha256": plan_sha256, **item})
            try:
                client.delete_object(Bucket=bucket, Key=item["key"], IfMatch=item["etag"])
                try:
                    _head(client, bucket, item["key"])
                except ClientError as error:
                    status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                    if status != 404:
                        raise
                else:
                    raise RuntimeError("deleted key still exists")
            except BaseException:
                _journal(journal, {"event": "outcome_unknown", "key": item["key"]})
                raise
            _journal(journal, {"event": "confirmed_absent", "key": item["key"]})
            report["deleted"].append(item["key"])
    guard()
    if _pointers(client, bucket) != expected:
        raise ValueError("current pointers changed at final check")
    for group in plan["groups"]:
        _check_receipt(client, bucket, group["keep"])
    return report


def main() -> None:
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
    from baibai_engine.storage.objects.r2 import Boto3R2Store
    from baibai_jobs.publication.lease import acquire, release, verify_held

    store = Boto3R2Store(bucket="baibai-stores")
    try:
        if not args.apply:
            print(json.dumps(run(store._client(), plan), ensure_ascii=False))
            return
        if not args.journal:
            raise ValueError("apply requires journal path")
        actual_account = hashlib.sha256(store.endpoint.encode()).hexdigest()
        if actual_account != plan.get("account_endpoint_sha256"):
            raise ValueError("R2 account endpoint differs from the approved destination")
        # The journal stores metadata and outcomes, not a copy of the data.
        fd = os.open(args.journal, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        directory_fd = os.open(args.journal.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        with os.fdopen(fd, "w", encoding="utf-8") as journal:
            _journal(journal, {"event": "start", "plan_sha256": args.plan_sha256})
            handle, expired_previous = acquire(store, "operator")
            try:
                if expired_previous:
                    raise RuntimeError("previous publication lease expired; investigate first")

                def guard() -> None:
                    verify_held(store, handle)
                    _github_idle()
                    verify_held(store, handle)

                guard()
                report = run(
                    store._client(),
                    plan,
                    apply=True,
                    guard=guard,
                    journal=journal,
                    plan_sha256=args.plan_sha256,
                )
                _journal(journal, {"event": "completed", "objects": len(report["deleted"])})
                print(json.dumps(report, ensure_ascii=False))
            finally:
                release(store, handle)
    finally:
        store.close()


if __name__ == "__main__":
    main()
