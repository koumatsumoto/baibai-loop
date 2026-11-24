"""Offline adversaries for the latest-only scoped input read boundary."""

from __future__ import annotations

import json
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.receipts.native_sources import store as sqlite_store
from baibai_engine.data_collection.receipts.native_sources.records import store_snapshot
from baibai_engine.storage.objects.r2 import LakePublishError, R2Error
from baibai_jobs.acquisition.source_inputs import input_key, load_input, publish_input

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

DAY = date(2026, 11, 23)
WHEN = datetime(2026, 11, 23, 9, tzinfo=UTC)
GROUP = "economic-jquants"
FRED = "economic-fred_csv"
SOURCE = "jquants-indices-topix"
DAYS = "economic_jquants_indices_topix_input_days"
PREFIX = "coordination/source-inputs/"
RAW = "coordination/economic-fred-sources/fred_csv-other/handoff.json"


class Remote(MemoryR2Store):
    """Inject a real pause at the checked-manifest/HEAD/streaming GET boundary."""

    def __init__(self) -> None:
        super().__init__()
        self.interrupt: Callable[[str, str], None] | None = None

    def iter_objects(self, prefix):
        pytest.fail(f"scoped reads/publishes must never scan {prefix}")

    def _before(self, phase, key):
        if self.interrupt is not None:
            self.interrupt(phase, key)

    def get_checked_object(self, key, *, max_bytes):
        self._before("manifest_get", key)
        response = self.get_bytes_with_metadata(key)
        if response is not None:
            raw, meta = response
            assert len(raw) <= max_bytes
            assert len(raw) == meta.size
        return response

    def head(self, key):
        self._before("head", key)
        return super().head(key)

    def download_file(self, key, path, *, expect_bytes=None):
        self._before("body_get", key)
        if key not in self.values:
            raise R2Error("test: GET returned not found", http_status=404)
        super().download_file(key, path, expect_bytes=expect_bytes)


def manifest_key(stage: str) -> str:
    return PREFIX + stage + "/handoff.json"


def stage_data(remote: Remote, stage: str):
    return json.loads(remote.values[manifest_key(stage)].body)


def physical(manifest: dict[str, object]) -> str:
    return str(manifest.get("planning_key") or manifest["receipt_key"])


def insert_day(path: Path, day: date) -> None:
    with closing(sqlite_store.open_connection(path)) as conn, conn:
        store_snapshot(
            conn,
            source_id=SOURCE,
            observed_at=day,
            fetched_at=WHEN,
            records=[{"Date": day.isoformat(), "C": "42"}],
        )


@pytest.fixture
def subject(tmp_path):
    remote = Remote()
    source = tmp_path / "source.sqlite"
    with closing(sqlite_store.initialize_database(source)):
        pass
    insert_day(source, DAY)
    remote.store_object(RAW, b"raw-original")

    def publish(identity, *, group=GROUP, asof=DAY, etag="v1"):
        return publish_input(
            remote,
            market=tmp_path / "unused",
            economic=source,
            group=group,
            asof=asof,
            identity=identity,
            generations={"economic_etag": etag},
        )

    return remote, source, publish


def inspect_bound(tmp_path, remote, location, stage, *, asof, etag, expected_days):
    output = tmp_path / (location + ".sqlite")
    result = load_input(remote, GROUP, output, tmp_path / location, expected_stage=stage)
    assert result["asof"] == asof.isoformat()
    assert result["canonical_generations"] == {"economic_etag": etag}
    with closing(sqlite_store.open_read_only_connection(output)) as conn:
        assert [
            row[0] for row in conn.execute(f"SELECT observed_at FROM {DAYS} ORDER BY observed_at")
        ] == [d.isoformat() for d in expected_days]
        assert conn.execute("SELECT count(*) FROM economic_source_records").fetchone()[0] == 0
    return result


@pytest.mark.parametrize("binding", ["asof_only", "etag_only"])
def test_same_projection_does_not_justify_suppressing_new_binding(subject, tmp_path, binding):
    remote, _, publish = subject
    a = publish("a")
    before = list(remote.put_keys)
    assert publish("unused") == a
    assert remote.put_keys == before
    asof = DAY + timedelta(days=1) if binding == "asof_only" else DAY
    etag = "v2" if binding == "etag_only" else "v1"
    b = publish("b", asof=asof, etag=etag)
    assert b != a
    assert remote.put_keys[len(before) :] == [manifest_key(b), input_key(GROUP)]
    data_a, data_b = stage_data(remote, a), stage_data(remote, b)
    assert physical(data_a) == physical(data_b)
    assert data_b["receipt_format"] == "jquants-acquisition-input-v1"
    assert "planning_key" in data_b
    assert "receipt_key" not in data_b
    assert data_b["result"]["created_at"] >= data_a["result"]["created_at"]
    inspect_bound(tmp_path, remote, "b", b, asof=asof, etag=etag, expected_days=[DAY])


@pytest.mark.parametrize("expected_stage", [False, True])
@pytest.mark.parametrize("phase", ["manifest_get", "head", "body_get"])
def test_a_body_borrowed_by_b_remains_live_until_c_then_retry_exact_latest_set(
    subject,
    tmp_path,
    phase,
    expected_stage,
):
    remote, source, publish = subject
    a = publish("a")
    a_body = physical(stage_data(remote, a))
    b = publish("b", etag="v2")
    assert physical(stage_data(remote, b)) == a_body
    # Completion of A→B retires A's manifest, but not the exact cross-stage object.
    del remote.values[manifest_key(a)]
    assert a_body in remote.values
    inspect_bound(tmp_path, remote, "before", b, asof=DAY, etag="v2", expected_days=[DAY])
    legacy_fred = publish("same-scope-is-not-global", group=FRED)
    fred_body = physical(stage_data(remote, legacy_fred))
    before_fred = remote.values[fred_body]
    next_day = DAY + timedelta(days=1)
    c_body = None

    def advance(read_phase, key):
        nonlocal c_body
        target = manifest_key(b) if phase == "manifest_get" else a_body
        if (read_phase, key) != (phase, target):
            return
        remote.interrupt = None
        insert_day(source, next_day)
        c = publish("c", asof=next_day, etag="v3")
        c_body = physical(stage_data(remote, c))
        assert c_body != a_body
        inspect_bound(
            tmp_path,
            remote,
            "checked-c",
            c,
            asof=next_day,
            etag="v3",
            expected_days=[DAY, next_day],
        )
        # B→C retires exactly B's manifest and B's now unreachable body under A.
        del remote.values[manifest_key(b)]
        del remote.values[a_body]

    remote.interrupt = advance
    if expected_stage:
        with pytest.raises(LakePublishError):
            inspect_bound(tmp_path, remote, "paused", b, asof=DAY, etag="v2", expected_days=[DAY])
        assert not (tmp_path / "paused.sqlite").exists()
    else:
        inspect_bound(
            tmp_path,
            remote,
            "paused",
            None,
            asof=next_day,
            etag="v3",
            expected_days=[DAY, next_day],
        )
    assert c_body in remote.values
    assert remote.values[fred_body] is before_fred
    assert remote.values[RAW].body == b"raw-original"


def test_receipt_key_missing_manifest_not_confused_with_fred_raw_wire_format(subject, tmp_path):
    remote, _, publish = subject
    a = publish("a-fred", group=FRED)
    body = physical(stage_data(remote, a))
    key = input_key(FRED)

    def advance(phase, requested):
        if (phase, requested) == ("manifest_get", manifest_key(a)):
            remote.interrupt = None
            b = publish("b-fred", group=FRED, etag="v2")
            manifest = stage_data(remote, b)
            assert manifest["receipt_key"] == body
            assert "planning_key" not in manifest
            assert "receipt_format" not in manifest
            del remote.values[manifest_key(a)]

    remote.interrupt = advance
    report = load_input(remote, FRED, None, tmp_path / "latest")
    assert report["canonical_generations"] == {"economic_etag": "v2"}
    assert remote.values[body].body.startswith(b"SQLite format 3\x00")
    assert remote.values[RAW].body == b"raw-original"
    assert key in remote.values


@pytest.mark.parametrize("failure", ["unknown_status", "403", "body_content", "head_metadata"])
def test_advancing_pointer_does_not_hide_old_corruption_or_transport_errors(
    subject, tmp_path, failure
):
    remote, source, publish = subject
    a = publish("a")
    a_body = physical(stage_data(remote, a))

    def corrupt(phase, key):
        expected = "head" if failure == "head_metadata" else "body_get"
        if (phase, key) != (expected, a_body):
            return
        remote.interrupt = None
        insert_day(source, DAY + timedelta(days=1))
        publish("next", etag="v2")
        if failure in {"unknown_status", "403"}:
            raise R2Error(
                "read operation failed", http_status=None if failure == "unknown_status" else 403
            )
        if failure == "head_metadata":
            remote.values[a_body].metadata["sha256"] = "0" * 64
        else:
            remote.values[a_body].body = b"?" * len(remote.values[a_body].body)

    remote.interrupt = corrupt
    with pytest.raises(LakePublishError):
        load_input(remote, GROUP, tmp_path / "bad.sqlite", tmp_path / "bad")
    assert not (tmp_path / "bad.sqlite").exists()


@pytest.mark.parametrize("same_stage_changed_pointer", [False, True])
def test_current_missing_body_fails_even_if_only_pointer_metadata_was_rewritten(
    subject,
    tmp_path,
    same_stage_changed_pointer,
):
    remote, _, publish = subject
    stage = publish("a")
    a_body = physical(stage_data(remote, stage))
    key = input_key(GROUP)

    def remove(phase, requested):
        if (phase, requested) == ("body_get", a_body):
            remote.interrupt = None
            if same_stage_changed_pointer:
                pointer = json.loads(remote.values[key].body)
                # A transport ETag change does not imply a different immutable selector.
                pointer["non_selection_note"] = "changed"
                remote.store_object(key, json.dumps(pointer).encode())
            del remote.values[a_body]

    remote.interrupt = remove
    with pytest.raises(LakePublishError):
        load_input(remote, GROUP, tmp_path / "missing.sqlite", tmp_path / "missing")
    assert remote.get_keys.count(a_body) == 0  # Raced first GET, no new body attempt.


def test_retry_is_bounded_when_two_new_stages_overtake_reader(subject, tmp_path):
    remote, _, publish = subject
    a = publish("a")
    a_body = physical(stage_data(remote, a))
    pointer = input_key(GROUP)
    writer = Mock()

    def shift_from(stage, next_stage, etag, next_hook):
        remote.interrupt = None
        publish(next_stage, etag=etag)
        del remote.values[manifest_key(stage)]
        writer()
        remote.interrupt = next_hook

    def b_to_c(phase, key):
        if (phase, key) == ("manifest_get", manifest_key(GROUP + "-b")):
            shift_from(GROUP + "-b", "c", "v3", None)

    def a_to_b(phase, key):
        if (phase, key) == ("manifest_get", manifest_key(a)):
            shift_from(a, "b", "v2", b_to_c)

    remote.interrupt = a_to_b
    with pytest.raises(LakePublishError):
        load_input(remote, GROUP, tmp_path / "raced.sqlite", tmp_path / "raced")
    assert writer.call_count == 2
    assert json.loads(remote.values[pointer].body)["stage_id"] == GROUP + "-c"
    assert a_body in remote.values
    assert not (tmp_path / "raced.sqlite").exists()


@pytest.mark.parametrize("strict", [False, True])
def test_intact_bound_read_is_not_a_final_head_fence(subject, tmp_path, strict):
    remote, _, publish = subject
    a = publish("a")
    a_body = physical(stage_data(remote, a))

    def advance(phase, key):
        if (phase, key) == ("body_get", a_body):
            remote.interrupt = None
            # B reuses A: the acquired input is still a correct snapshot, but not current.
            publish("b", etag="v2")

    remote.interrupt = advance
    inspect_bound(
        tmp_path, remote, "old", a if strict else None, asof=DAY, etag="v1", expected_days=[DAY]
    )
    with pytest.raises(LakePublishError, match="changed during readback"):
        inspect_bound(tmp_path, remote, "not-latest", a, asof=DAY, etag="v1", expected_days=[DAY])
    inspect_bound(tmp_path, remote, "fresh", GROUP + "-b", asof=DAY, etag="v2", expected_days=[DAY])


@pytest.mark.parametrize(
    "invalid", ["generations", "group", "format", "zero_size", "negative_size"]
)
def test_advancing_pointer_does_not_hide_invalid_old_binding_or_format(subject, tmp_path, invalid):
    remote, source, publish = subject
    a = publish("a")
    a_body = physical(stage_data(remote, a))

    def corrupt(phase, key):
        if (phase, key) != ("manifest_get", manifest_key(a)):
            return
        remote.interrupt = None
        insert_day(source, DAY + timedelta(days=1))
        publish("b", etag="v2")
        record = stage_data(remote, a)
        if invalid == "generations":
            record["result"]["canonical_generations"] = {"economic_etag": "inconsistent"}
        elif invalid == "group":
            record["result"]["group"] = FRED
        elif invalid == "format":
            record["receipt_format"] = "fred-acquisition-receipts-zstd-v1"
        else:
            record["receipt_bytes"] = 0 if invalid == "zero_size" else -1
        remote.store_object(manifest_key(a), json.dumps(record).encode())
        del remote.values[a_body]

    remote.interrupt = corrupt
    if invalid == "format":
        error = pytest.raises(ValueError, match="unsupported receipt format")
    elif invalid in {"zero_size", "negative_size"}:
        error = pytest.raises(ValueError, match="invalid source handoff manifest")
    else:
        error = pytest.raises(LakePublishError, match="generation binding")
    with error:
        load_input(remote, GROUP, tmp_path / "bad.sqlite", tmp_path / "bad")
    assert not (tmp_path / "bad.sqlite").exists()


def test_lost_ack_noncurrent_uuid_can_still_supply_current_input(subject, tmp_path, monkeypatch):
    remote, source, publish = subject
    a = publish("a")
    a_body = physical(stage_data(remote, a))
    next_day = DAY + timedelta(days=1)
    insert_day(source, next_day)
    actual = remote.put_file
    ptr = input_key(GROUP)

    def lose_ack(key, *args, **kwargs):
        result = actual(key, *args, **kwargs)
        if key == ptr:
            monkeypatch.setattr(remote, "put_file", actual)
            publish("c", etag="v3")  # Commits C using B before B sees the response.
            raise R2Error("test: B committed, but response was lost")
        return result

    monkeypatch.setattr(remote, "put_file", lose_ack)
    with pytest.raises(R2Error, match="response was lost"):
        publish("b", etag="v2")
    b = GROUP + "-b"
    c = GROUP + "-c"
    b_body = physical(stage_data(remote, b))
    assert b_body != a_body
    assert physical(stage_data(remote, c)) == b_body
    assert json.loads(remote.values[ptr].body)["stage_id"] == c
    # A stage being noncurrent does not imply owning no live data.
    del remote.values[manifest_key(b)]
    inspect_bound(tmp_path, remote, "safe", c, asof=DAY, etag="v3", expected_days=[DAY, next_day])
    del remote.values[b_body]  # A naive cleanup of the lost ACK's UUID is destructive.
    with pytest.raises(LakePublishError, match="body is missing"):
        load_input(remote, GROUP, None, tmp_path / "broken", expected_stage=c)


def test_same_asof_etag_still_compares_real_projection_bytes(subject, tmp_path):
    remote, source, publish = subject
    a = publish("a")
    before = list(remote.put_keys)
    next_day = DAY + timedelta(days=1)
    insert_day(source, next_day)
    b = publish("b")
    assert b != a
    body_b = physical(stage_data(remote, b))
    assert physical(stage_data(remote, a)) != body_b
    assert remote.put_keys[len(before) :] == [body_b, manifest_key(b), input_key(GROUP)]
    inspect_bound(
        tmp_path,
        remote,
        "different-projection",
        b,
        asof=DAY,
        etag="v1",
        expected_days=[DAY, next_day],
    )
