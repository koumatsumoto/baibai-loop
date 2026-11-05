from pathlib import Path

import pytest

from baibai_engine.storage.snapshots import inventory as inventory_owner
from baibai_engine.storage.snapshots.inventory import inventory


def test_inventory_reads_metadata_only_and_groups_valid_keys(tmp_path: Path, monkeypatch) -> None:
    key = (
        "lake/l2/processed/jquants.daily_bars/contract=v1/"
        f"year=2026/month=8/part-{'e' * 64}.parquet"
    )
    object_path = tmp_path / key
    object_path.parent.mkdir(parents=True)
    object_path.write_bytes(b"parquet")
    (tmp_path / "market").mkdir()
    (tmp_path / "market" / "market.sqlite").write_bytes(b"store")
    (tmp_path / "outside.txt").write_text("ignored", encoding="utf-8")

    def no_file_contents(*args, **kwargs):
        pytest.fail("inventory must not read or write file contents")

    monkeypatch.setattr(Path, "open", no_file_contents)
    result = inventory(tmp_path)

    assert result["objects"] == 1
    assert result["bytes"] == 7
    assert result["areas"] == [
        {
            "prefix": "lake/l2/processed/jquants.daily_bars",
            "objects": 1,
            "bytes": 7,
        }
    ]
    assert result["invalid_keys"] == []


def test_inventory_reports_an_unsafe_key_inside_the_namespace(tmp_path: Path) -> None:
    stray = tmp_path / "lake" / "l2" / "processed" / "jquants daily bars" / "part.parquet"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"parquet")

    result = inventory(tmp_path)

    assert result["objects"] == 1
    assert result["invalid_keys"] == ["lake/l2/processed/jquants daily bars/part.parquet"]
    assert result["areas"] == []


def test_inventory_reports_every_class_against_its_own_budget(tmp_path: Path) -> None:
    published = tmp_path / (
        "lake/l2/processed/jquants.daily_bars/contract=v1/year=2026/month=8/"
        f"part-{'a' * 64}.parquet"
    )
    published.parent.mkdir(parents=True)
    published.write_bytes(b"parquet")
    failed = tmp_path / "lake/staging/failed-build/part.parquet"
    failed.parent.mkdir(parents=True)
    failed.write_bytes(b"quarantined")
    staged = tmp_path / "lake/staging/snapshot-1/snapshot.sqlite"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"sealed snapshot")

    (tmp_path / ".lake-writer.lock").write_bytes(b"")

    report = inventory(tmp_path)
    capacity = {item["class"]: item for item in report["capacity"]}

    assert report["invalid_keys"] == []
    assert report["control_files"] == {"objects": 1, "bytes": 0}
    assert report["objects"] == 3

    assert set(capacity) == {"published", "workspace"}
    assert capacity["published"]["bytes"] == 7
    assert capacity["workspace"]["bytes"] == 26
    assert capacity["published"]["soft_budget_bytes"] == 10 * 1024**3
    assert not any(item["budget_exceeded"] for item in capacity.values())


def test_inventory_consumes_each_path_before_requesting_the_next(tmp_path, monkeypatch):
    keys = [
        "lake/l1/source/example/part.parquet",
        "lake/l2/processed/example/part.parquet",
        "lake/manifests/releases/market/release.json",
        "lake/pointers/market/current.json",
        "lake/staging/build/source.sqlite",
        "lake/other/record.json",
        "lake/b?/part",
        "lake/b?-/part",
    ]
    paths = [tmp_path / key for key in keys]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"data")
    (tmp_path / ".lake-writer.lock").write_bytes(b"lock")
    expected = inventory(tmp_path)
    inspected = set()
    is_file = Path.is_file

    def inspect(path):
        inspected.add(path)
        return is_file(path)

    def walk(root, pattern):
        assert root == tmp_path / "lake"
        assert pattern == "*"
        for path in reversed(paths):
            yield path
            assert path in inspected

    monkeypatch.setattr(Path, "rglob", walk)
    monkeypatch.setattr(Path, "is_file", inspect)
    actual = inventory(tmp_path)

    assert actual == expected
    assert actual["invalid_keys"] == ["lake/b?/part", "lake/b?-/part"]


@pytest.mark.parametrize("exists", [False, True])
def test_inventory_distinguishes_missing_and_empty_roots(tmp_path, exists):
    root = tmp_path / "mirror"
    if exists:
        root.mkdir()

    result = inventory(root)

    assert result["exists"] is exists
    assert result["objects"] == result["bytes"] == 0
    assert result["areas"] == result["invalid_keys"] == []
    assert result["control_files"] == {"objects": 0, "bytes": 0}
    assert bool(result["capacity"]) is exists
    assert root.exists() is exists


def test_inventory_rejects_a_file_root(tmp_path):
    root = tmp_path / "not-a-directory"
    root.write_bytes(b"store")
    with pytest.raises(ValueError, match="root must be a directory"):
        inventory(root)


@pytest.mark.parametrize(("size", "exceeded"), [(2, False), (3, True)])
def test_inventory_budget_is_exceeded_only_above_the_limit(tmp_path, monkeypatch, size, exceeded):
    path = tmp_path / "lake/staging/build/source.sqlite"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"x" * size)
    monkeypatch.setattr(inventory_owner, "SOFT_BUDGET_BYTES", {"published": 2, "workspace": 2})

    report = inventory(tmp_path)
    capacity = {item["class"]: item for item in report["capacity"]}

    assert capacity["workspace"]["budget_exceeded"] is exceeded
    assert capacity["published"]["bytes"] == 0
    assert capacity["published"]["budget_exceeded"] is False
