from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from baibai_jobs.maintenance import edinet_archive_cleanup as cleanup


def fixture_plan(tmp_path: Path) -> dict[str, Any]:
    prefix = "lake/l1/source/edinet.source_archives/contract=v1/year=2026/month=10/part-"
    keep = {"key": prefix + "a" * 64 + ".parquet", "sha256": "a" * 64, "etag": '"a"', "size": 3}
    targets = []
    for n in range(12):
        sha = f"{n + 1:064x}"
        size = 4_038_186_716 if n < 11 else 48_458_240_597 - 11 * 4_038_186_716
        item = {"key": prefix + sha + ".parquet", "sha256": sha, "etag": f'"{n}"', "size": size}
        proof = {
            "old_key": item["key"],
            "old_file_sha256": sha,
            "current_key": keep["key"],
            "current_file_sha256": keep["sha256"],
            "old_HEAD_after": {"ETag": item["etag"], "bytes": size},
            "current_HEAD_after": {"ETag": keep["etag"], "bytes": keep["size"]},
            "old_is_exact_row_subset_of_current": True,
            "all_fields_and_raw_bytes_equal": 7,
            "old_rows": 7,
            "current_rows": 8,
            "schema_and_metadata_equal": True,
            "missing_in_current": 0,
            "changed_rows": 0,
            "comparison_fields": ["doc_id", "source_type", "stored_on", "zip_content"],
        }
        raw = json.dumps(proof).encode()
        name = f"{sha}.json"
        (tmp_path / name).write_bytes(raw)
        targets.append({**item, "proof": name, "proof_sha256": hashlib.sha256(raw).hexdigest()})
    return {
        "bucket": cleanup.BUCKET,
        "keep": keep,
        "targets": targets,
        "pointer": {},
        "current_release_key": "current",
        "active_pointers": {},
        "dataset_index": {},
        "release_index": {"release": "etag"},
    }


class Client:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        assert Bucket == cleanup.BUCKET
        self.deleted.append(Key)


def setup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cleanup, "_current", lambda *args: None)
    monkeypatch.setattr(cleanup, "_active_pointers", lambda *args: None)
    monkeypatch.setattr(cleanup, "_historical_evidence", lambda *args: None)
    monkeypatch.setattr(cleanup, "_dataset_index", lambda *args: None)
    monkeypatch.setattr(cleanup, "_object", lambda *args: None)
    monkeypatch.setattr(cleanup, "_release_index", lambda *args: {"release": "etag"})
    monkeypatch.setattr(cleanup, "_verify_survivor_bytes", lambda *args: None)
    monkeypatch.setattr(cleanup, "_require_absent", lambda *args: None)
    monkeypatch.setattr(
        cleanup,
        "_delete_once",
        lambda client, key: client.delete_object(Bucket=cleanup.BUCKET, Key=key),
    )


def test_dry_run_and_exact_deletion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    setup(monkeypatch)
    plan = fixture_plan(tmp_path)
    client = Client()
    result = cleanup.run(client, plan, tmp_path)
    assert result["objects"] == 12
    assert result["bytes"] == 48_458_240_597
    assert client.deleted == []
    with (tmp_path / "journal").open("w") as journal:
        cleanup.run(
            client,
            plan,
            tmp_path,
            apply=True,
            guard=lambda: None,
            journal=journal,
            plan_sha256="sha",
        )
    assert client.deleted == [item["key"] for item in plan["targets"]]
    events = [json.loads(line)["event"] for line in (tmp_path / "journal").read_text().splitlines()]
    assert events == ["intent", "confirmed_absent"] * 12


def test_changed_proof_or_inventory_never_deletes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup(monkeypatch)
    plan = fixture_plan(tmp_path)
    client = Client()
    (tmp_path / plan["targets"][-1]["proof"]).write_text("{}")
    with (
        (tmp_path / "journal").open("w") as journal,
        pytest.raises(ValueError, match="proof changed"),
    ):
        cleanup.run(
            client,
            plan,
            tmp_path,
            apply=True,
            guard=lambda: None,
            journal=journal,
            plan_sha256="sha",
        )
    assert client.deleted == []
    plan = fixture_plan(tmp_path)
    monkeypatch.setattr(cleanup, "_release_index", lambda *args: {"new": "etag"})
    with pytest.raises(ValueError, match="inventory changed"):
        cleanup.run(client, plan, tmp_path)
    assert client.deleted == []


def test_reject_current_target_and_running_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup(monkeypatch)
    plan = fixture_plan(tmp_path)
    client = Client()
    plan["keep"]["key"] = plan["targets"][0]["key"]
    with pytest.raises(ValueError, match="current"):
        cleanup.run(client, plan, tmp_path)
    plan = fixture_plan(tmp_path)

    def active() -> None:
        raise RuntimeError("batch active")

    with (
        (tmp_path / "journal").open("w") as journal,
        pytest.raises(RuntimeError, match="batch active"),
    ):
        cleanup.run(
            client, plan, tmp_path, apply=True, guard=active, journal=journal, plan_sha256="sha"
        )
    assert client.deleted == []


@pytest.mark.parametrize("status", [301, 307])
def test_redirect_is_blocked_before_second_wire_request(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    import boto3
    from botocore.awsrequest import AWSResponse
    from botocore.config import Config

    class EmptyBody:
        def stream(self, *_args: Any, **_kwargs: Any):
            yield b""

    client = boto3.client(
        "s3",
        endpoint_url="https://example.test",
        region_name="auto",
        aws_access_key_id="fake",
        aws_secret_access_key="fake",
        config=Config(retries={"total_max_attempts": 1}),
    )
    requests = []

    def redirect(request: Any) -> AWSResponse:
        requests.append(request)
        return AWSResponse(request.url, status, {"x-amz-bucket-region": "us-west-2"}, EmptyBody())

    monkeypatch.setattr(client._endpoint.http_session, "send", redirect)
    with pytest.raises(RuntimeError, match="second DELETE request was blocked"):
        cleanup._delete_once(client, "test-key")
    assert len(requests) == 1


@pytest.mark.parametrize("count", [True, "7", 6])
def test_proof_rejects_noninteger_or_incomplete_row_count(tmp_path: Path, count: object) -> None:
    plan = fixture_plan(tmp_path)
    item = plan["targets"][0]
    path = tmp_path / item["proof"]
    proof = json.loads(path.read_text())
    proof["all_fields_and_raw_bytes_equal"] = count
    raw = json.dumps(proof).encode()
    path.write_bytes(raw)
    item["proof_sha256"] = hashlib.sha256(raw).hexdigest()
    with pytest.raises(ValueError, match="proof does not establish"):
        cleanup._proof(path, item, plan["keep"])


def test_historical_evidence_rejects_extra_affected_release(tmp_path: Path) -> None:
    plan = fixture_plan(tmp_path)
    items = [
        {"key": x["key"], "release_keys": [], "current_pointer_references": []}
        for x in plan["targets"]
    ]
    items[0]["release_keys"] = [f"lake/manifests/releases/{i}" for i in range(40)]
    proof = {
        "old_files": items,
        "old_referencing_noncurrent_distinct_release_count": 40,
        "release_listing_stable": True,
        "dataset_listing_stable": True,
        "pointers_stable": True,
        "failure": None,
        "manifest_proofs": [
            *[{"key": f"lake/manifests/releases/{i}", "etag": "old"} for i in range(40)],
            {"key": "lake/manifests/datasets/old", "etag": "data"},
        ],
    }
    plan.update(
        current_release_key="current",
        release_index={
            "current": "x",
            **{f"lake/manifests/releases/{i}": "old" for i in range(40)},
        },
        dataset_index={"lake/manifests/datasets/old": "data"},
        reference_evidence="reference.json",
    )
    path = tmp_path / "reference.json"

    def write() -> None:
        raw = json.dumps(proof).encode()
        path.write_bytes(raw)
        plan["reference_evidence_sha256"] = hashlib.sha256(raw).hexdigest()

    write()
    keys = {item["key"] for item in plan["targets"]}
    cleanup._historical_evidence(plan, tmp_path, keys)
    items[0]["release_keys"][0] = "unexpected"
    write()
    with pytest.raises(ValueError, match="historical manifest evidence differs"):
        cleanup._historical_evidence(plan, tmp_path, keys)
    items[0]["release_keys"].append("unexpected-41")
    write()
    with pytest.raises(ValueError, match="historical release impact differs"):
        cleanup._historical_evidence(plan, tmp_path, keys)


def test_current_rejects_changed_release_key(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = json.dumps({"manifest_key": "unexpected"}).encode()
    monkeypatch.setattr(cleanup, "_read", lambda *_args: (raw, "etag"))
    with pytest.raises(ValueError, match="current release identity differs"):
        cleanup._current(
            None, {"etag": "etag", "sha256": hashlib.sha256(raw).hexdigest()}, {}, "current"
        )


def test_dataset_change_after_first_delete_stops_remaining(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup(monkeypatch)
    plan = fixture_plan(tmp_path)
    client = Client()
    calls = 0

    def dataset_check(*_args: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise ValueError("dataset changed")

    monkeypatch.setattr(cleanup, "_dataset_index", dataset_check)
    with (
        (tmp_path / "journal").open("w") as journal,
        pytest.raises(ValueError, match="dataset changed"),
    ):
        cleanup.run(
            client,
            plan,
            tmp_path,
            apply=True,
            guard=lambda: None,
            journal=journal,
            plan_sha256="sha",
        )
    assert client.deleted == [plan["targets"][0]["key"]]
    events = [json.loads(line)["event"] for line in (tmp_path / "journal").read_text().splitlines()]
    assert events == ["intent", "confirmed_absent"]


def test_uncertain_delete_stops_and_journals_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup(monkeypatch)
    plan = fixture_plan(tmp_path)
    client = Client()

    def uncertain(*_args: object) -> None:
        raise RuntimeError("second DELETE request was blocked")

    monkeypatch.setattr(cleanup, "_delete_once", uncertain)
    with (
        (tmp_path / "journal").open("w") as journal,
        pytest.raises(RuntimeError, match="second DELETE"),
    ):
        cleanup.run(
            client,
            plan,
            tmp_path,
            apply=True,
            guard=lambda: None,
            journal=journal,
            plan_sha256="sha",
        )
    assert client.deleted == []
    events = [json.loads(line)["event"] for line in (tmp_path / "journal").read_text().splitlines()]
    assert events == ["intent", "outcome_unknown"]
