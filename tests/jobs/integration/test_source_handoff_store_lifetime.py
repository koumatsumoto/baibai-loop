"""The source-handoff CLI owns and releases only the store it actually uses."""

import json

import pytest

from baibai_jobs.acquisition import receipts
from baibai_jobs.integration import source_handoff


@pytest.mark.parametrize("command", ["resume", "stage", "download"])
@pytest.mark.parametrize("failure", [None, OSError, KeyboardInterrupt])
def test_owned_store_closes_on_success_failure_and_interrupt(
    tmp_path, monkeypatch, capsys, command, failure
):
    source = "tradingview" if command == "resume" else "jpx-earnings"
    result = tmp_path / "result.json"
    saved = {"created_at": "2026-11-17T00:00:00+00:00"}
    if command == "resume":
        from tests.helpers.tradingview import DAY, native_inputs, store

        _, master, calendar = native_inputs(store(tmp_path / "market.sqlite", 1), tmp_path, day=DAY)
        saved.update(
            master_native_input=master.model_dump(mode="json"),
            calendar_native_input=calendar.model_dump(mode="json"),
        )
    result.write_text(json.dumps(saved))
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
    monkeypatch.setattr(receipts, "available_handoffs", transfer)
    args = [command, "--source", source, "--directory", str(tmp_path)]
    args += ["--asof", "2026-11-17"] if command == "resume" else ["--stage-id", "fixed-stage"]
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


@pytest.mark.parametrize(
    ("source", "payload", "status"),
    [("tradingview", "{}", 0), ("tradingview", "{", 1)],
)
def test_resume_checks_saved_input_before_constructing_remote(
    tmp_path, monkeypatch, capsys, source, payload, status
):
    result = tmp_path / "result.json"
    result.write_text(payload)

    def unexpected(**kwargs):
        raise AssertionError("remote store should not be constructed")

    monkeypatch.setattr(source_handoff, "Boto3R2Store", unexpected)
    assert (
        source_handoff.main(
            ["resume", "--source", source, "--directory", str(tmp_path), "--asof", "2026-11-17"]
        )
        == status
    )
    assert result.read_text() == payload
    assert not list(tmp_path.glob("resume-*"))
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ("source handoff failed: JSONDecodeError\n" if status else "")


def test_stage_output_failure_still_closes_store(tmp_path, monkeypatch, capsys):
    events = []

    class Store:
        def __init__(self, **kwargs):
            events.append("open")

        def close(self):
            events.append("close")

    monkeypatch.setattr(source_handoff, "Boto3R2Store", Store)
    monkeypatch.setattr(source_handoff, "save_handoff", lambda *a, **k: events.append("saved"))
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


@pytest.mark.parametrize("command", ["stage", "resume"])
def test_missing_command_argument_does_not_construct_store(tmp_path, monkeypatch, command):
    def unexpected(**kwargs):
        raise AssertionError("remote store should not be constructed")

    monkeypatch.setattr(source_handoff, "Boto3R2Store", unexpected)
    assert (
        source_handoff.main([command, "--source", "tradingview", "--directory", str(tmp_path)]) == 1
    )
