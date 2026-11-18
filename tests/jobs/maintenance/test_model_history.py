import gzip
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from tests.helpers.model_generation import empty_selection, generation, publish_generation

from baibai_engine.machine_analysis.generation.query import read_generation
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore
from baibai_jobs import cli
from baibai_jobs.maintenance import model_history


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def publish(path, identity):
    item = generation().model_copy(update={"generation_id": identity})
    publish_generation(item, path=path)
    ScreeningSelectionStore(path).publish(f"selection-{identity}", empty_selection(identity))


def execute(path, sql, parameters=()):
    with closing(sqlite3.connect(path)) as connection, connection:
        return connection.execute(sql, parameters).fetchall()


def rows(path, table):
    return sorted(execute(path, f"SELECT * FROM {table}"))


@pytest.fixture
def snapshots(tmp_path):
    source, current = tmp_path / "source.sqlite", tmp_path / "current.sqlite"
    for identity in ("old", "shared", "unrequested"):
        publish(source, identity)
    for identity in ("shared", "new"):
        publish(current, identity)
    # Publication clocks and compressed bytes must be identical for the overlap.
    shared = execute(source, "SELECT * FROM screening_selection WHERE generation_id='shared'")[0]
    execute(
        current,
        "UPDATE screening_selection SET published_at=?,payload_gzip=? WHERE selection_id=?",
        (shared[2], shared[3], shared[0]),
    )
    ScreeningSelectionStore(current).publish("new-policy-shared", empty_selection("shared"))
    execute(current, "CREATE TABLE other_current_state (value TEXT)")
    execute(current, "INSERT INTO other_current_state VALUES ('current-only')")
    return source, current


def restore(snapshots, output, **kwargs):
    source, current = snapshots
    return model_history.restore_model_history(
        source=source,
        current=current,
        output=output,
        source_sha256=kwargs.pop("source_sha256", digest(source)),
        current_sha256=kwargs.pop("current_sha256", digest(current)),
        generation_ids=kwargs.pop("generation_ids", ("old", "shared")),
        **kwargs,
    )


def test_restore_adds_missing_raw_history_preserving_all_current_rows(snapshots, tmp_path):
    source, current = snapshots
    original = execute(
        source, "SELECT payload_gzip FROM generated_model WHERE generation_id='old' LIMIT 1"
    )[0][0]
    unusual_gzip = gzip.compress(gzip.decompress(original), mtime=17)
    execute(
        source,
        "UPDATE generated_model SET payload_gzip=? WHERE generation_id='old' "
        "AND model_id=(SELECT model_id FROM generated_model WHERE generation_id='old' LIMIT 1)",
        (unusual_gzip,),
    )
    before = digest(source), digest(current)
    output = tmp_path / "union.sqlite"
    report = restore(snapshots, output)
    assert report["status"] == "prepared_local_history_copy"
    assert report["added_generation_ids"] == ["old"]
    assert report["verified_existing_generation_ids"] == ["shared"]
    assert report["added_model_count"] == 3
    assert report["added_selection_ids"] == ["selection-old"]
    assert report["output_sha256"] == digest(output)
    for table in ("model_generation", "generated_model", "screening_selection"):
        expected = set(rows(current, table)) | {
            row for row in rows(source, table) if row[0] == "old" or row[0] == "selection-old"
        }
        assert set(rows(output, table)) == expected
    assert rows(output, "other_current_state") == rows(current, "other_current_state")
    assert before == (digest(source), digest(current))
    assert read_generation("old", path=output) is not None
    assert read_generation("new", path=output) == read_generation("new", path=current)
    assert ScreeningSelectionStore(output).read("selection-old") is not None
    assert ScreeningSelectionStore(output).read("new-policy-shared") is not None
    assert read_generation("unrequested", path=output) is None

    repeated = tmp_path / "repeated.sqlite"
    again = restore((source, output), repeated)
    assert again["added_generation_ids"] == []
    assert again["added_model_count"] == 0
    assert again["added_selection_ids"] == []
    for table in (
        "model_generation",
        "generated_model",
        "screening_selection",
        "other_current_state",
    ):
        assert rows(repeated, table) == rows(output, table)


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE model_generation SET header=header||' ' WHERE generation_id='shared'",
        "UPDATE model_generation SET evaluation_time='2026-10-26T00:00:00+00:00' WHERE generation_id='shared'",
        "UPDATE generated_model SET calculation_revision='other' WHERE generation_id='shared'",
        "UPDATE generated_model SET payload_gzip=x'1234' WHERE generation_id='shared'",
        "DELETE FROM generated_model WHERE generation_id='shared' AND model_id='company_financials'",
        (
            "INSERT INTO generated_model SELECT generation_id,'extra',subject,calculation_revision,payload_gzip "
            "FROM generated_model WHERE generation_id='shared' AND model_id='company_financials'"
        ),
        "UPDATE screening_selection SET published_at='2026-10-26T00:00:00+00:00' WHERE generation_id='shared'",
        "UPDATE screening_selection SET payload_gzip=x'1234' WHERE generation_id='shared'",
        "UPDATE screening_selection SET generation_id='new' WHERE selection_id='selection-shared'",
    ],
)
def test_any_overlapping_conflict_rejects_without_changing_either_input(snapshots, tmp_path, sql):
    source, current = snapshots
    execute(current, sql)
    before = digest(source), digest(current)
    output = tmp_path / "union.sqlite"
    with pytest.raises(ValueError, match="conflicting"):
        restore(snapshots, output)
    assert not output.exists()
    assert before == (digest(source), digest(current))


def test_same_decoded_payload_with_different_gzip_is_still_a_conflict(snapshots, tmp_path):
    source, current = snapshots
    payload = execute(
        source,
        "SELECT payload_gzip FROM generated_model WHERE "
        "generation_id='shared' AND model_id='company_financials'",
    )[0][0]
    execute(
        current,
        "UPDATE generated_model SET payload_gzip=? WHERE generation_id='shared' "
        "AND model_id='company_financials'",
        (gzip.compress(gzip.decompress(payload), mtime=1),),
    )
    with pytest.raises(ValueError, match="conflicting generated_model"):
        restore(snapshots, tmp_path / "out")


def test_missing_selection_can_be_added_to_exact_existing_generation(snapshots, tmp_path):
    _, current = snapshots
    execute(current, "DELETE FROM screening_selection WHERE selection_id='selection-shared'")
    output = tmp_path / "union.sqlite"
    report = restore(snapshots, output, generation_ids=("shared",))
    assert report["added_generation_ids"] == []
    assert report["added_selection_ids"] == ["selection-shared"]
    assert ScreeningSelectionStore(output).read("new-policy-shared") is not None


@pytest.mark.parametrize("ids", [(), ("",), ("old", "old"), ("old", "absent")])
def test_explicit_distinct_present_generation_set_is_required(snapshots, tmp_path, ids):
    with pytest.raises(ValueError, match="generation"):
        restore(snapshots, tmp_path / "out", generation_ids=ids)
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_sha256", "invalid"),
        ("source_sha256", "0" * 64),
        ("current_sha256", "0" * 64),
    ],
)
def test_verified_input_hashes_are_required(snapshots, tmp_path, field, value):
    with pytest.raises(ValueError, match="SHA256"):
        restore(snapshots, tmp_path / "out", **{field: value})
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("which", [0, 1])
@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_input_sidecars_are_rejected(snapshots, tmp_path, which, suffix):
    Path(str(snapshots[which]) + suffix).write_bytes(b"uncheckpointed")
    with pytest.raises(ValueError, match="standalone"):
        restore(snapshots, tmp_path / "out")


@pytest.mark.parametrize(
    "sql",
    [
        "PRAGMA user_version=7",
        (
            "CREATE TRIGGER no_append AFTER INSERT ON model_generation BEGIN "
            "DELETE FROM generated_model; END"
        ),
    ],
)
def test_schema_migration_and_trigger_execution_are_not_recovery(snapshots, tmp_path, sql):
    execute(snapshots[0], sql)
    with pytest.raises(ValueError, match=r"schema|triggers"):
        restore(snapshots, tmp_path / "out")


@pytest.mark.parametrize("kind", ["current", "source", "file", "symlink", "hardlink", "dangling"])
def test_existing_output_or_alias_is_never_overwritten(snapshots, tmp_path, kind):
    source, current = snapshots
    output = tmp_path / "out"
    if kind in ("source", "current"):
        output = source if kind == "source" else current
    elif kind == "symlink":
        output.symlink_to(current)
    elif kind == "dangling":
        output.symlink_to(tmp_path / "absent")
    elif kind == "hardlink":
        output.hardlink_to(current)
    else:
        output.write_bytes(b"keep")
    before = digest(source), digest(current)
    with pytest.raises(FileExistsError):
        restore(snapshots, output)
    assert before == (digest(source), digest(current))
    assert output.exists() or output.is_symlink()


def test_same_input_or_orphan_output_sidecar_is_rejected(snapshots, tmp_path):
    source, _ = snapshots
    with pytest.raises(ValueError, match="distinct"):
        restore((source, source), tmp_path / "out")
    Path(str(tmp_path / "out") + "-wal").write_bytes(b"keep")
    with pytest.raises(ValueError, match="output has SQLite sidecars"):
        restore(snapshots, tmp_path / "out")


def test_failure_after_inserts_discards_only_new_output(snapshots, tmp_path, monkeypatch):
    before = tuple(map(digest, snapshots))
    append = model_history._append_history

    def fail(connection, ids):
        append(connection, ids)
        assert connection.execute(
            "SELECT 1 FROM model_generation WHERE generation_id='old'"
        ).fetchone()
        raise RuntimeError("synthetic after-insert failure")

    monkeypatch.setattr(model_history, "_append_history", fail)
    with pytest.raises(RuntimeError, match="after-insert"):
        restore(snapshots, tmp_path / "out")
    assert not list(tmp_path.glob("out*"))
    assert before == tuple(map(digest, snapshots))


def test_source_attachment_is_read_only(snapshots, tmp_path, monkeypatch):
    def attempt_write(connection, ids):
        connection.execute("DELETE FROM history.screening_selection")

    before = tuple(map(digest, snapshots))
    monkeypatch.setattr(model_history, "_append_history", attempt_write)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        restore(snapshots, tmp_path / "out")
    assert before == tuple(map(digest, snapshots))
    assert not (tmp_path / "out").exists()


def test_input_change_during_preparation_is_not_accepted(snapshots, tmp_path, monkeypatch):
    append = model_history._append_history

    def change_input(connection, ids):
        result = append(connection, ids)
        execute(snapshots[1], "INSERT INTO other_current_state VALUES ('changed')")
        return result

    monkeypatch.setattr(model_history, "_append_history", change_input)
    with pytest.raises(ValueError, match="input snapshot changed"):
        restore(snapshots, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_invalid_current_foreign_key_is_not_certified(snapshots, tmp_path):
    execute(
        snapshots[1],
        "UPDATE screening_selection SET generation_id='absent' WHERE selection_id='selection-new'",
    )
    with pytest.raises(ValueError, match="invalid foreign keys"):
        restore(snapshots, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_standalone_wal_format_inputs_do_not_create_input_sidecars(snapshots, tmp_path):
    for path in snapshots:
        execute(path, "PRAGMA journal_mode=WAL")
    before = tuple(map(digest, snapshots))
    restore(snapshots, tmp_path / "out")
    assert before == tuple(map(digest, snapshots))
    assert not list(tmp_path.glob("*.sqlite-*"))
    assert not list(tmp_path.glob("out-*"))


def test_cli_is_local_preparation_not_publication(snapshots, tmp_path, capsys):
    source, current = snapshots
    output = tmp_path / "out"
    assert (
        cli.main(
            [
                "restore-model-history",
                "--source",
                str(source),
                "--current",
                str(current),
                "--output",
                str(output),
                "--source-sha256",
                digest(source),
                "--current-sha256",
                digest(current),
                "--generation-id",
                "old",
                "--generation-id",
                "shared",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "prepared_local_history_copy"
    assert report["added_model_count"] == 3


def test_partial_growth_restore_does_not_require_its_unselected_storage_parent(tmp_path):
    from baibai_engine.machine_analysis.generation.contracts import GeneratedModel
    from baibai_engine.storage.sqlite.machine_analysis import initialize_run_store

    source, current, output = (tmp_path / n for n in ("source", "current", "output"))
    initialize_run_store(current)
    proof = [hashlib.sha256(str(i).encode()).hexdigest() for i in range(180)]
    for n in ("early", "later"):
        model = GeneratedModel(
            model_id="company_growth",
            subject="1234",
            calculation_revision="fixed",
            payload={"originals": proof, "input_release": n},
        )
        publish_generation(
            generation().model_copy(update={"generation_id": n, "models": (model,)}), path=source
        )
    saved = execute(source, "SELECT payload_gzip FROM generated_model WHERE generation_id='later'")[
        0
    ][0]
    assert not saved.startswith(b"\x1f\x8b")
    report = restore((source, current), output, generation_ids=("later",))
    assert report["added_model_count"] == 1
    assert read_generation("early", path=output) is None
    assert read_generation("later", path=output) == read_generation("later", path=source)
    again = restore((source, output), tmp_path / "twice", generation_ids=("later",))
    assert again["added_model_count"] == 0

    execute(source, "DELETE FROM generated_model WHERE generation_id='early'")
    with pytest.raises(ValueError, match="missing or changed"):
        restore((source, output), tmp_path / "refused", generation_ids=("later",))
    assert not (tmp_path / "refused").exists()


def test_unknown_tag_within_growth_reference_namespace_is_not_a_portable_full_row(tmp_path):
    from baibai_engine.storage.sqlite.machine_analysis import initialize_run_store

    source, current = tmp_path / "source", tmp_path / "current"
    initialize_run_store(current)
    publish(source, "available")
    execute(
        source,
        "UPDATE generated_model SET model_id='company_growth',payload_gzip=? "
        "WHERE model_id='company_financials'",
        (b"\0growth/9\0other",),
    )
    with pytest.raises(ValueError, match="unknown company growth storage"):
        restore((source, current), tmp_path / "refused", generation_ids=("available",))
    assert not (tmp_path / "refused").exists()
