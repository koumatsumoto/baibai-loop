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
            "all_fields_and_raw_bytes_equal": True,
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
        "active_pointers": {},
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
    monkeypatch.setattr(cleanup, "_object", lambda *args: None)
    monkeypatch.setattr(cleanup, "_release_index", lambda *args: {"release": "etag"})
    monkeypatch.setattr(cleanup, "_verify_survivor_bytes", lambda *args: None)
    monkeypatch.setattr(cleanup, "_require_absent", lambda *args: None)


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
