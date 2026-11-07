"""本番・providerを使わず、有限raw診断と既存ownerでの回復境界を確認する。"""

import json
import shutil
from contextlib import closing
from datetime import UTC, date, datetime, timedelta

import pytest
from tests.helpers.r2_store import MemoryR2Store
from tools.diagnostics import inspect_economic_confirmation_gaps as diagnosis

from baibai_engine.data_collection.receipts.native_sources.confirmations import confirm_snapshot
from baibai_engine.data_collection.receipts.native_sources.handoff import accept_rechecks
from baibai_engine.data_collection.receipts.native_sources.records import (
    read_snapshot_rows,
    store_snapshot,
)
from baibai_engine.data_collection.receipts.native_sources.store import initialize_database
from baibai_engine.storage.snapshots.sources import sha256_file
from baibai_jobs.acquisition.economic_staging import save_handoff, stage_prefix
from baibai_jobs.acquisition.receipts import available_handoffs, seal_integrated
from baibai_jobs.integration.native_source_merge import merge_stores, verify_stores

SOURCE = "nikkei-pbr"
DAY = date(2026, 11, 6)
AT = datetime(2026, 11, 6, 10, tzinfo=UTC)
PRIVATE = "synthetic-provider-value-never-print"


def receive(path, value, hour, *, day=DAY, source_id=SOURCE, confirmed=False):
    with closing(initialize_database(path)) as conn, conn:
        (confirm_snapshot if confirmed else store_snapshot)(
            conn,
            source_id=source_id,
            observed_at=day,
            fetched_at=AT + timedelta(hours=hour),
            records=[] if value is None else [{"weighted_text": value, "private": PRIVATE}],
        )


def stage(tmp_path, rows, *, identity="test-stage", marker="integrated", source_status="ok"):
    directory = tmp_path / identity
    directory.mkdir()
    raw = directory / "receipts.sqlite"
    for value, hour, options in rows:
        receive(raw, value, hour, **options)
    result = {
        "group": "nikkei_indexes",
        # Deliberately outside the receipt window: this is not the receipt clock.
        "started_at": (AT - timedelta(days=4)).isoformat(),
        "asof": DAY.isoformat(),
        "sources": [{"source_id": SOURCE, "status": source_status}],
    }
    (directory / "result.json").write_text(json.dumps(result))
    store = MemoryR2Store()
    save_handoff(store, identity, directory)
    entry = available_handoffs(store)[0]
    if marker is not None:
        seal_integrated(store, entry, generations={}, recovery=marker == "needs_explicit_recovery")
    prefix = stage_prefix(identity)
    (directory / "handoff.json").write_bytes(store.get_bytes(prefix + "/handoff.json"))
    if marker is not None:
        (directory / "integrated.json").write_bytes(store.get_bytes(prefix + "/integrated.json"))
    return diagnosis.Stage(identity, sha256_file(directory / "handoff.json"), directory)


def inspect(canonical, stages, **kwargs):
    return diagnosis.inspect(
        canonical,
        **{
            "canonical_sha256": sha256_file(canonical),
            "stages": stages,
            "source_id": SOURCE,
            "start": DAY,
            "end": DAY,
            "received_since": AT,
            "received_until": AT + timedelta(hours=6),
            **kwargs,
        },
    )


@pytest.mark.parametrize("value", ["A", None])
def test_clock_gap_is_a_candidate_and_owner_replay_preserves_originals(tmp_path, value):
    canonical = tmp_path / "canonical.sqlite"
    receive(canonical, value, 0)
    packet = stage(tmp_path, [(value, 3, {})])
    originals = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    report = inspect(canonical, (packet,))
    assert report["counts"] == {"confirmation_gap_candidate": 1}
    witness = report["findings"][0]["latest_receipt_in_window"]
    assert witness["marker_status"] == "integrated"
    assert witness["fetched_at"] == (AT + timedelta(hours=3)).isoformat(timespec="microseconds")
    assert report["coverage"] == "not_established"
    assert report["production_impact"] == "unverified"
    assert report["recovery"] == "not_performed"
    assert PRIVATE not in json.dumps(report)
    assert originals == {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    # This copy/replay belongs only to the fixture, not the diagnostic CLI.
    candidate = tmp_path / "candidate.sqlite"
    shutil.copyfile(canonical, candidate)
    selected = tmp_path / "selected.sqlite"
    result = accept_rechecks(packet.directory / "receipts.sqlite", candidate, selected)
    assert not result["late_recovery_required"]
    merge_stores(selected, candidate)
    verify_stores(selected, candidate)
    with closing(initialize_database(candidate)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM economic_source_records").fetchone()[0] == 1
        check = conn.execute(
            "SELECT checked_at,adopted_fetched_at FROM economic_source_record_confirmations"
        ).fetchone()
        assert tuple(check) == tuple(
            (AT + timedelta(hours=h)).isoformat(timespec="microseconds") for h in (3, 0)
        )
    assert inspect(candidate, (packet,))["counts"] == {"current_matches": 1}
    assert all(path.read_bytes() == body for path, body in originals.items())


def test_late_b_adoption_is_detected_and_raw_a_repairs_only_a_fixture_copy(tmp_path):
    canonical = tmp_path / "canonical.sqlite"
    receive(canonical, "A", 0)
    receive(canonical, "B", 2)
    packet = stage(tmp_path, [("A", 3, {})])
    assert inspect(canonical, (packet,))["counts"] == {"adoption_gap_candidate": 1}
    candidate = tmp_path / "candidate.sqlite"
    shutil.copyfile(canonical, candidate)
    selected = tmp_path / "selected.sqlite"
    assert not accept_rechecks(packet.directory / "receipts.sqlite", candidate, selected)[
        "late_recovery_required"
    ]
    merge_stores(selected, candidate)
    verify_stores(selected, candidate)
    with closing(initialize_database(candidate)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM economic_source_records").fetchone()[0] == 3
        assert next(read_snapshot_rows(conn, DAY, DAY, source_id=SOURCE))[2] == (
            AT + timedelta(hours=3)
        ).isoformat(timespec="microseconds")
    assert inspect(candidate, (packet,))["counts"] == {"current_matches": 1}


def test_newer_canonical_is_not_a_rollback_candidate(tmp_path):
    canonical = tmp_path / "canonical.sqlite"
    receive(canonical, "B", 4, confirmed=True)
    packet = stage(tmp_path, [("A", 3, {})])
    assert inspect(canonical, (packet,))["counts"] == {"canonical_newer": 1}


@pytest.mark.parametrize("marker", [None, "needs_explicit_recovery"])
def test_unsupplied_or_recovery_marker_is_not_claimed_integrated(tmp_path, marker):
    canonical = tmp_path / "canonical.sqlite"
    receive(canonical, "A", 0)
    packet = stage(tmp_path, [("A", 3, {})], marker=marker, source_status="failed")
    report = inspect(canonical, (packet,))
    assert report["counts"] == {"confirmation_gap_candidate": 1}
    assert report["handoffs"][0]["marker_status"] == (marker or "not_supplied")
    assert report["coverage"] == "not_established"


def test_missing_day_and_unselected_source_are_not_empty_receipts(tmp_path):
    canonical = tmp_path / "canonical.sqlite"
    receive(canonical, "A", 0)
    receive(canonical, "other", 0, source_id="nikkei-per")
    tomorrow = DAY + timedelta(days=1)
    receive(canonical, "A", 0, day=tomorrow)
    packet = stage(tmp_path, [("A", 3, {})])
    report = inspect(canonical, (packet,), end=tomorrow)
    assert [row["status"] for row in report["findings"]] == [
        "confirmation_gap_candidate",
        "no_raw_evidence",
    ]
    assert report["findings"][1]["latest_receipt_in_window"] is None


def test_confirmation_clock_is_not_replaced_by_its_older_anchor(tmp_path):
    canonical = tmp_path / "canonical.sqlite"
    receive(canonical, "A", 0)
    packet = stage(tmp_path, [("A", 0, {"confirmed": True}), ("A", 3, {"confirmed": True})])
    report = inspect(canonical, (packet,), received_since=AT + timedelta(hours=1))
    assert report["counts"] == {"confirmation_gap_candidate": 1}
    row = report["findings"][0]["latest_receipt_in_window"]
    assert row["fetched_at"] == AT.isoformat(timespec="microseconds")
    assert row["checked_at"] == (AT + timedelta(hours=3)).isoformat(timespec="microseconds")


@pytest.mark.parametrize("canonical_conflict", [True, False])
def test_same_clock_conflicts_are_reported_even_behind_newer_canonical(
    tmp_path, canonical_conflict
):
    canonical = tmp_path / "canonical.sqlite"
    receive(canonical, "A", 0)
    if canonical_conflict:
        receive(canonical, "B", 3)
    receive(canonical, "C", 4, confirmed=True)
    packets = [stage(tmp_path, [("A", 3, {})])]
    if not canonical_conflict:
        packets.append(stage(tmp_path, [("B", 3, {})], identity="other-stage"))
    report = inspect(canonical, tuple(packets))
    assert report["counts"] == {"conflicting_receipt": 1}
    assert len(report["findings"][0]["conflicting_clocks"]) == 1


def test_finite_receipt_window_does_not_claim_latest_over_unsupplied_history(tmp_path):
    canonical = tmp_path / "canonical.sqlite"
    receive(canonical, "B", 2)
    packet = stage(tmp_path, [("A", 3, {}), ("B", 5, {})])
    report = inspect(canonical, (packet,), received_until=AT + timedelta(hours=4))
    assert report["counts"] == {"adoption_gap_candidate": 1}
    assert report["coverage"] == "not_established"
    assert inspect(canonical, (packet,))["counts"] == {"confirmation_gap_candidate": 1}


@pytest.mark.parametrize("target", ["canonical", "manifest", "receipt", "marker", "schema"])
def test_bad_fixed_inputs_fail_without_changing_inputs(tmp_path, target):
    canonical = tmp_path / "canonical.sqlite"
    receive(canonical, "A", 0)
    packet = stage(tmp_path, [("A", 3, {})])
    kwargs = {}
    if target == "canonical":
        kwargs["canonical_sha256"] = "0" * 64
    elif target == "manifest":
        packet = diagnosis.Stage(packet.stage_id, "0" * 64, packet.directory)
    elif target == "receipt":
        (packet.directory / "receipts.sqlite").write_bytes(b"bad-original")
    elif target == "marker":
        path = packet.directory / "integrated.json"
        value = json.loads(path.read_text())
        value["manifest_sha256"] = "0" * 64
        path.write_text(json.dumps(value))
    else:
        with closing(initialize_database(canonical)) as conn:
            conn.execute("PRAGMA user_version=12")
    originals = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    with pytest.raises((ValueError, RuntimeError)):
        inspect(canonical, (packet,), **kwargs)
    assert all(path.read_bytes() == body for path, body in originals.items())


def test_local_change_during_inspection_is_not_reported_as_success(tmp_path, monkeypatch):
    canonical = tmp_path / "canonical.sqlite"
    receive(canonical, "A", 0)
    packet = stage(tmp_path, [("A", 3, {})])
    original = diagnosis._finding

    def change(*args, **kwargs):
        result = original(*args, **kwargs)
        canonical.write_bytes(b"changed-copy")
        return result

    monkeypatch.setattr(diagnosis, "_finding", change)
    with pytest.raises(ValueError, match="input changed"):
        inspect(canonical, (packet,))


def cli_args(canonical, packet):
    return [
        "--canonical",
        str(canonical),
        "--canonical-sha256",
        sha256_file(canonical),
        "--stage",
        packet.stage_id,
        packet.manifest_sha256,
        str(packet.directory),
        "--source-id",
        SOURCE,
        "--start",
        str(DAY),
        "--end",
        str(DAY),
        "--received-since",
        AT.isoformat(),
        "--received-until",
        (AT + timedelta(hours=6)).isoformat(),
    ]


def test_local_cli_has_no_network_or_recovery_and_redacts_failure(tmp_path, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("local diagnostic must not use a real R2 client")

    monkeypatch.setattr("baibai_engine.storage.objects.r2.Boto3R2Store", forbidden)
    canonical = tmp_path / "canonical.sqlite"
    receive(canonical, "A", 0)
    packet = stage(tmp_path, [("A", 3, {})])
    args = cli_args(canonical, packet)
    assert diagnosis.main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["recovery"] == "not_performed"
    args[3] = "0" * 64
    assert diagnosis.main(args) == 1
    assert json.loads(capsys.readouterr().out) == {"status": "failed", "reason": "ValueError"}


@pytest.mark.parametrize(
    "changes",
    [
        {"source_id": "us-cpi"},
        {"start": DAY + timedelta(days=1)},
        {"received_since": AT.replace(tzinfo=None)},
        {"stages": ()},
    ],
)
def test_unbounded_or_nonrange_scope_is_rejected(tmp_path, changes):
    canonical = tmp_path / "canonical.sqlite"
    receive(canonical, "A", 0)
    packet = stage(tmp_path, [("A", 3, {})])
    stages = changes.get("stages", (packet,))
    with pytest.raises(ValueError, match=r"required|require|supported"):
        inspect(
            canonical, stages, **{key: value for key, value in changes.items() if key != "stages"}
        )


@pytest.mark.parametrize(
    "kind", ["marker_list", "sources_shape", "truncated_gzip", "private_status"]
)
def test_cli_contains_malformed_input_errors_without_payload_output(tmp_path, capsys, kind):
    canonical = tmp_path / "canonical.sqlite"
    receive(canonical, "A", 0)
    packet = stage(
        tmp_path,
        [("A", 3, {})],
        marker=None,
        source_status=PRIVATE if kind == "private_status" else "ok",
    )
    if kind == "marker_list":
        (packet.directory / "integrated.json").write_text("[]")
    elif kind in {"sources_shape", "truncated_gzip"}:
        path = packet.directory / "handoff.json"
        manifest = json.loads(path.read_text())
        if kind == "sources_shape":
            manifest["result"]["sources"] = [None]
        else:
            raw = packet.directory / "receipts.sqlite"
            with closing(initialize_database(raw)) as conn, conn:
                conn.execute("UPDATE economic_source_records SET payload=?", (b"\x1f\x8b",))
            digest = sha256_file(raw)
            manifest.update(
                receipt_sha256=digest,
                receipt_bytes=raw.stat().st_size,
                receipt_key=stage_prefix(packet.stage_id) + f"/receipts-{digest}.sqlite",
            )
        path.write_text(json.dumps(manifest))
        packet = diagnosis.Stage(packet.stage_id, sha256_file(path), packet.directory)
    originals = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert diagnosis.main(cli_args(canonical, packet)) == 1
    output = capsys.readouterr()
    assert not output.err
    assert PRIVATE not in output.out
    assert json.loads(output.out) == {
        "status": "failed",
        "reason": {
            "marker_list": "AttributeError",
            "sources_shape": "TypeError",
            "truncated_gzip": "EOFError",
            "private_status": "ValueError",
        }[kind],
    }
    assert all(path.read_bytes() == body for path, body in originals.items())
