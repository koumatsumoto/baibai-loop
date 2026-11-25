"""The common integration command owns its client, not its borrowed wrappers."""

import json
from types import SimpleNamespace

import pytest

from baibai_engine.storage.objects.r2 import R2Error, R2OperationStopped
from baibai_jobs.integration import sources


def _case(tmp_path, monkeypatch, command, failure=None, *, failed_input=False):
    events, stores = [], []
    directory = tmp_path / "integration"
    report = {"handoffs": [], "extractions": {}, "external": None, "external_failed": []}
    if command == "seal":
        directory.mkdir()
        report["handoffs"] = [
            {
                "entry": {"market": True, "source": "jquants-prices", "stage_id": "fixed"},
                "applied": True,
                "result": {},
                "recovery": False,
            }
        ]
        (directory / "applied.json").write_text(json.dumps(report))

    class Store:
        def __init__(self, **kwargs):
            assert kwargs == {"bucket": "synthetic-bucket"}
            self.closed = False
            events.append("construct")
            stores.append(self)

        def close(self):
            assert not self.closed
            self.closed = True
            events.append("close")

    def use(store, stage):
        assert getattr(store, "store", store) is stores[0]
        assert not stores[0].closed
        events.append(stage)
        if stage == command and failure is not None:
            raise failure("PRIVATE_CANARY")

    def ready(store):
        use(store, "plan")
        return [], []

    def apply(store, *args, **kwargs):
        use(store, "apply")
        return report

    def publish(store, source, generations):
        use(store, "inputs")
        if failed_input:
            raise ValueError("synthetic article planning failure")

    def seal(store, *args, **kwargs):
        use(store, "seal")

    monkeypatch.setenv("R2_STORES_BUCKET", "synthetic-bucket")
    monkeypatch.setattr(sources, "Boto3R2Store", Store)
    monkeypatch.setattr(sources, "available_ready", ready)
    monkeypatch.setattr(sources, "available_handoffs", lambda *_, **__: [])
    monkeypatch.setattr(sources, "pending_handoffs", lambda *_: [])
    monkeypatch.setattr(
        sources, "fix_edinet_input", lambda *_: SimpleNamespace(model_dump=lambda **_: {"fixed": 1})
    )
    monkeypatch.setattr(sources, "apply", apply)
    monkeypatch.setattr(sources, "_load_handle", lambda _: object())
    monkeypatch.setattr(sources, "verify_held", lambda store, _: use(store, "lease"))
    monkeypatch.setattr(sources, "canonical_generations", lambda _: {"lake_release_id": "fixed"})
    monkeypatch.setattr(sources, "publish_planning_input", publish)
    monkeypatch.setattr(sources, "seal_integrated", seal)
    args = [command, "--directory", str(directory)]
    if command in {"apply", "inputs"}:
        args += ["--asof", "2026-11-17"]
    if command in {"inputs", "seal"}:
        args += ["--lease-handle", str(tmp_path / "lease.json")]
    if command == "inputs":
        args += ["--identity", "fixed", "--group", "tsr_flash"]
    return args, events, stores


@pytest.mark.parametrize("command", ["plan", "apply", "inputs", "seal"])
@pytest.mark.parametrize("failure", [None, OSError, KeyboardInterrupt, R2OperationStopped])
def test_owned_store_closes_after_success_or_interruption(
    tmp_path, monkeypatch, capsys, command, failure
):
    args, events, stores = _case(tmp_path, monkeypatch, command, failure)
    marker = tmp_path / "retained"
    marker.write_bytes(b"prior canonical data")
    if failure in {KeyboardInterrupt, R2OperationStopped}:
        with pytest.raises(failure, match="PRIVATE_CANARY"):
            sources.main(args)
    else:
        assert sources.main(args) == int(failure is not None)
    assert events == ["construct"] + (["lease"] if command in {"inputs", "seal"} else []) + [
        command,
        "close",
    ]
    assert len(stores) == 1
    assert stores[0].closed
    assert marker.read_bytes() == b"prior canonical data"
    output = capsys.readouterr()
    assert "PRIVATE_CANARY" not in output.out + output.err
    if failure is OSError:
        assert output.err.endswith(f"common source integration failed: {failure.__name__}\n")


def test_failed_input_report_also_closes_before_return(tmp_path, monkeypatch, capsys):
    args, events, stores = _case(tmp_path, monkeypatch, "inputs", failed_input=True)
    assert sources.main(args) == 1
    assert json.loads(capsys.readouterr().out) == {
        "updated": [],
        "failed": [{"group": "tsr_flash", "reason": "ValueError"}],
    }
    assert events == ["construct", "lease", "inputs", "close"]
    assert stores[0].closed


@pytest.mark.parametrize("command", ["plan", "apply", "inputs", "seal"])
def test_existing_plan_or_missing_required_argument_still_closes(tmp_path, monkeypatch, command):
    args, events, stores = _case(tmp_path, monkeypatch, command)
    if command == "plan":
        directory = tmp_path / "integration"
        directory.mkdir()
        (directory / "plan.json").write_bytes(b"fixed plan")
    else:
        option = "--asof" if command == "apply" else "--lease-handle"
        index = args.index(option)
        del args[index : index + 2]
    assert sources.main(args) == 1
    assert events == ["construct", "close"]
    assert stores[0].closed
    if command == "plan":
        assert (directory / "plan.json").read_bytes() == b"fixed plan"


@pytest.mark.parametrize("command", ["apply", "inputs"])
def test_output_failure_closes_without_repeating_work(tmp_path, monkeypatch, capsys, command):
    args, events, stores = _case(tmp_path, monkeypatch, command)

    def fail_output(*args, **kwargs):
        if "file" not in kwargs:
            raise OSError("PRIVATE_CANARY")

    monkeypatch.setattr(sources, "print", fail_output, raising=False)
    assert sources.main(args) == 1
    assert events.count(command) == 1
    assert events[-1] == "close"
    assert stores[0].closed
    assert "PRIVATE_CANARY" not in capsys.readouterr().err


def test_directory_failure_closes_constructed_store(tmp_path, monkeypatch):
    args, events, stores = _case(tmp_path, monkeypatch, "plan")
    (tmp_path / "integration").write_bytes(b"existing file")
    assert sources.main(args) == 1
    assert events == ["construct", "close"]
    assert stores[0].closed
    assert (tmp_path / "integration").read_bytes() == b"existing file"


def test_unfinished_constructor_has_no_owned_store_to_close(tmp_path, monkeypatch, capsys):
    args, events, stores = _case(tmp_path, monkeypatch, "plan")

    def fail(**kwargs):
        events.append("constructor-failed")
        raise OSError("PRIVATE_CANARY")

    monkeypatch.setattr(sources, "Boto3R2Store", fail)
    assert sources.main(args) == 1
    assert events == ["constructor-failed"]
    assert stores == []
    assert not (tmp_path / "integration").exists()
    assert capsys.readouterr().err == "common source integration failed: OSError\n"


def test_close_failure_does_not_repeat_successful_apply(tmp_path, monkeypatch, capsys):
    args, events, stores = _case(tmp_path, monkeypatch, "apply")
    factory = sources.Boto3R2Store

    def close():
        events.append("close-failed")
        raise OSError("PRIVATE_CANARY")

    def store(**kwargs):
        value = factory(**kwargs)
        value.close = close
        return value

    monkeypatch.setattr(sources, "Boto3R2Store", store)
    assert sources.main(args) == 1
    assert events == ["construct", "apply", "close-failed"]
    assert len(stores) == 1
    output = capsys.readouterr()
    assert json.loads(output.out)["handoffs"] == []
    assert output.err == "common source integration failed: OSError\n"


@pytest.mark.parametrize(
    ("phase", "boundary", "status"),
    [
        ("canonical_machine", "canonical_generations", 503),
        ("jpx_readback", "verify_jpx_publication", None),
        ("external_readback_and_seal", "verify_and_seal", None),
        ("handoff_terminal", "seal_integrated", None),
    ],
)
def test_seal_logs_only_safe_phase_and_r2_exception_class_once(
    tmp_path, monkeypatch, capsys, phase, boundary, status
):
    args, events, _ = _case(tmp_path, monkeypatch, "seal")
    path = tmp_path / "integration" / "applied.json"
    report = json.loads(path.read_bytes())
    if phase == "jpx_readback":
        stage = report["handoffs"][0]
        stage["entry"]["source"] = "jpx-earnings"
        stage["result"]["retained_bundles"] = ["PRIVATE_BUNDLE"]
    if phase == "external_readback_and_seal":
        report["external"] = {"plans": ["PRIVATE_PLAN"]}
    payload = json.dumps(report).encode()
    path.write_bytes(payload)

    def fail(*_args, **_kwargs):
        events.append("r2-failure")
        try:
            raise TimeoutError("PRIVATE_ENDPOINT")
        except TimeoutError as cause:
            raise R2Error("PRIVATE_KEY", http_status=status) from cause

    monkeypatch.setattr(sources, boundary, fail)
    assert sources.main(args) == 1
    assert events == ["construct", "lease", "r2-failure", "close"]
    assert path.read_bytes() == payload
    assert capsys.readouterr().err.splitlines() == [
        (
            f"source seal failed: phase={phase} exception=R2Error cause=TimeoutError "
            f"status={status if status is not None else 'unknown'}"
        ),
        "common source integration failed: R2Error",
    ]


def test_other_command_keeps_existing_r2_diagnostic(tmp_path, monkeypatch, capsys):
    args, events, _ = _case(tmp_path, monkeypatch, "plan", R2Error)
    assert sources.main(args) == 1
    assert events == ["construct", "plan", "close"]
    err = capsys.readouterr().err
    assert "source seal failed" not in err
    assert "PRIVATE_CANARY" not in err
    assert err.endswith("common source integration failed: R2Error\n")
