"""The source-handoff CLI owns and releases only the store it actually uses."""

import pytest

from baibai_jobs.integration import source_handoff


@pytest.mark.parametrize("command", ["stage", "download"])
@pytest.mark.parametrize("failure", [None, OSError, KeyboardInterrupt])
def test_owned_store_closes_on_success_failure_and_interrupt(
    tmp_path, monkeypatch, capsys, command, failure
):
    source = "tradingview" if command == "download" else "jpx-earnings"
    result = tmp_path / "result.json"
    result.write_text('{"bundle_ids": ["fixture-original"]}' if command == "stage" else "{}")
    original = result.read_bytes()
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    stores, events = [], []

    class Store:
        def __init__(self, **kwargs):
            self.closed = False
            stores.append(self)
            events.append("open")

        def close(self):
            assert not self.closed
            self.closed = True
            events.append("close")

    def transfer(store, *args, **kwargs):
        assert store is stores[0]
        assert not store.closed
        events.append("transfer")
        if failure is not None:
            raise failure("PRIVATECANARY")
        return []

    monkeypatch.setattr(source_handoff, "Boto3R2Store", Store)
    monkeypatch.setattr(source_handoff, "save_handoff", transfer)
    monkeypatch.setattr(source_handoff, "load_handoff", transfer)
    args = [command, "--source", source, "--directory", str(tmp_path), "--stage-id", "fixed-stage"]
    if failure is KeyboardInterrupt:
        with pytest.raises(KeyboardInterrupt, match="PRIVATECANARY"):
            source_handoff.main(args)
    else:
        assert source_handoff.main(args) == int(failure is not None)
    assert events == ["open", "transfer", "close"]
    assert len(stores) == 1
    assert stores[0].closed
    assert result.read_bytes() == original
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ("source handoff failed: OSError\n" if failure is OSError else "")
    if command == "stage" and failure is None:
        assert output.read_text() == "handoff=fixed-stage\n"
    else:
        assert not output.exists()


def test_stage_output_failure_still_closes_store(tmp_path, monkeypatch, capsys):
    events = []

    class Store:
        def __init__(self, **kwargs):
            events.append("open")

        def close(self):
            events.append("close")

    monkeypatch.setattr(source_handoff, "Boto3R2Store", Store)
    monkeypatch.setattr(source_handoff, "save_handoff", lambda *a, **k: events.append("saved"))
    (tmp_path / "result.json").write_text('{"bundle_ids": ["fixture-original"]}')
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path))
    assert (
        source_handoff.main(
            [
                "stage",
                "--source",
                "jpx-earnings",
                "--stage-id",
                "saved",
                "--directory",
                str(tmp_path),
            ]
        )
        == 1
    )
    assert events == ["open", "saved", "close"]
    assert capsys.readouterr().err == "source handoff failed: IsADirectoryError\n"


@pytest.mark.parametrize("command", ["stage", "download"])
def test_missing_stage_id_does_not_construct_store(tmp_path, monkeypatch, capsys, command):
    source = "tradingview" if command == "download" else "jpx-earnings"
    if command == "stage":
        (tmp_path / "result.json").write_text('{"bundle_ids": ["fixture-original"]}')

    def unexpected(**kwargs):
        pytest.fail("remote store should not be constructed")

    monkeypatch.setattr(source_handoff, "Boto3R2Store", unexpected)
    assert source_handoff.main([command, "--source", source, "--directory", str(tmp_path)]) == 1
    assert capsys.readouterr().err == "source handoff failed: ValueError\n"
