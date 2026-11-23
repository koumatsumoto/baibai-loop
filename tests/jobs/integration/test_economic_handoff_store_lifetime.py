"""The economic handoff command owns only its invocation's remote store."""

from contextlib import closing

import pytest
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.receipts.native_sources.store import initialize_database
from baibai_jobs.acquisition import source_inputs
from baibai_jobs.integration import economic_handoff


@pytest.mark.parametrize("command", ["input", "stage", "download"])
@pytest.mark.parametrize("failure", [None, OSError, KeyboardInterrupt])
def test_remote_store_closes_after_success_failure_and_interrupt(
    tmp_path, monkeypatch, capsys, command, failure
):
    events, stores = [], []
    marker = tmp_path / "result.json"
    marker.write_bytes(b'{"saved":"original"}')
    output = tmp_path / "github-output"
    database = tmp_path / "not-created.sqlite"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("R2_STORES_BUCKET", "synthetic-private-bucket")

    class Store:
        def __init__(self, **kwargs):
            assert kwargs == {"bucket": "synthetic-private-bucket"}
            self.closed = False
            stores.append(self)
            events.append("open")

        def close(self):
            assert not self.closed
            self.closed = True
            events.append("close")

    def transfer(store, *args):
        assert store is stores[0]
        assert not store.closed
        assert args == (
            ("economic-fred_csv", database, tmp_path)
            if command == "input"
            else ("fixed-stage", tmp_path)
        )
        events.append(command)
        if failure is not None:
            raise failure("PRIVATE_CANARY")
        return "fixed-stage"

    monkeypatch.setattr(economic_handoff, "Boto3R2Store", Store)
    monkeypatch.setattr(source_inputs, "load_input", transfer)
    monkeypatch.setattr(economic_handoff, "save_handoff", transfer)
    monkeypatch.setattr(economic_handoff, "load_handoff", transfer)
    args = [command, "--directory", str(tmp_path), "--group", "fred_csv", "--db", str(database)]
    if command != "input":
        args += ["--stage-id", "fixed-stage"]
    if failure is KeyboardInterrupt:
        with pytest.raises(KeyboardInterrupt, match="PRIVATE_CANARY"):
            economic_handoff.main(args)
    else:
        assert economic_handoff.main(args) == int(failure is not None)
    assert events == ["open", command, "close"]
    assert len(stores) == 1
    assert stores[0].closed
    assert marker.read_bytes() == b'{"saved":"original"}'
    assert not database.exists()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ("economic handoff failed: OSError\n" if failure is OSError else "")
    if command == "stage" and failure is None:
        assert output.read_text() == "handoff=fixed-stage\n"
    else:
        assert not output.exists()


def test_saved_stage_output_failure_closes_store(tmp_path, monkeypatch, capsys):
    events = []

    class Store:
        def __init__(self, **kwargs):
            events.append("open")

        def close(self):
            events.append("close")

    monkeypatch.setattr(economic_handoff, "Boto3R2Store", Store)
    monkeypatch.setattr(
        economic_handoff, "save_handoff", lambda *args: events.append("saved") or "fixed-stage"
    )
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path))
    assert (
        economic_handoff.main(["stage", "--directory", str(tmp_path), "--stage-id", "fixed-stage"])
        == 1
    )
    assert events == ["open", "saved", "close"]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "economic handoff failed: IsADirectoryError\n"


@pytest.mark.parametrize(
    "args",
    [
        ["input"],
        ["input", "--group", "unknown"],
        ["stage"],
        ["download"],
    ],
)
def test_invalid_local_arguments_do_not_construct_store(tmp_path, monkeypatch, args):
    def unexpected(**kwargs):
        raise AssertionError("remote store should not be constructed")

    monkeypatch.setattr(economic_handoff, "Boto3R2Store", unexpected)
    before = list(tmp_path.iterdir())
    args = list(args)
    if args != ["input"]:
        args += ["--directory", str(tmp_path)]
    assert economic_handoff.main(args) == 1
    assert list(tmp_path.iterdir()) == before


@pytest.mark.parametrize("command", ["input", "stage", "download"])
def test_constructor_failure_has_no_store_to_close(tmp_path, monkeypatch, capsys, command):
    events = []

    class Store:
        def __init__(self, **kwargs):
            events.append("construct")
            raise OSError("PRIVATE_CANARY")

        def close(self):
            raise AssertionError("construction did not return an owned store")

    monkeypatch.setattr(economic_handoff, "Boto3R2Store", Store)
    assert (
        economic_handoff.main(
            [command, "--directory", str(tmp_path), "--group", "fred_csv", "--stage-id", "fixed"]
        )
        == 1
    )
    assert events == ["construct"]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "economic handoff failed: OSError\n"


def test_download_output_failure_closes_store_without_discarding_receipt(
    tmp_path, monkeypatch, capsys
):
    events = []

    class Store(MemoryR2Store):
        def close(self):
            events.append("close")

    store = Store()
    source = tmp_path / "source"
    source.mkdir()
    with closing(initialize_database(source / "receipts.sqlite")):
        pass
    (source / "result.json").write_text('{"group":"boj","sources":[]}')
    economic_handoff.save_handoff(store, "fixed-stage", source)
    original = (source / "receipts.sqlite").read_bytes()
    puts = list(store.put_keys)
    target = tmp_path / "download"
    (target / "result.json").mkdir(parents=True)
    monkeypatch.setattr(economic_handoff, "Boto3R2Store", lambda **kwargs: store)
    assert (
        economic_handoff.main(["download", "--directory", str(target), "--stage-id", "fixed-stage"])
        == 1
    )
    assert events == ["close"]
    assert (target / "receipts.sqlite").read_bytes() == original
    assert (source / "receipts.sqlite").read_bytes() == original
    assert store.put_keys == puts
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "economic handoff failed: IsADirectoryError\n"
