from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
from contextlib import closing
from datetime import datetime
from pathlib import Path

import pytest
from tests.helpers.r2_transfer import (
    REPO_ROOT,
    TRANSFER_SCRIPT,
    _environment,
    _fake_aws,
    _fake_repo,
)

from baibai_engine.storage.sqlite.source_cache.lake_origin import (
    LakeStoreOrigin,
    write_lake_store_origin,
)
from baibai_engine.storage.sqlite.source_cache.schema import open_connection


def test_transfer_rejects_a_split_layout_before_loading_credentials(tmp_path: Path) -> None:
    root = _fake_repo(tmp_path)
    bin_dir, log = _fake_aws(tmp_path)
    environment = _environment(bin_dir, log)
    environment["LAYOUT_FAKE_EXIT"] = "2"
    for name in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        environment.pop(name)

    completed = subprocess.run(
        [root / "jobs/scripts/r2_transfer.sh", "pull-machine"],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert _transfer_commands(log) == []


def _transfer_commands(log: Path) -> list[str]:
    """The transfers the run performed, without the diagnostic calls.

    The publish path also asks the CLI for its version and for the concurrency it
    resolved, so a real run's log can answer whether the setting took effect. Those
    are not transfers and would otherwise have to be counted in every assertion.
    """
    if not log.exists():
        return []
    return [
        line
        for line in log.read_text(encoding="utf-8").splitlines()
        if line != "--version" and not line.startswith("configure get ")
    ]


def test_sqlite_helpers_run_from_the_repository_when_called_elsewhere(tmp_path: Path) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    root = _fake_repo(tmp_path)
    environment = _environment(bin_dir, log)
    environment["UV_EXPECTED_CWD"] = str(root)
    _install_fixture_receipt(environment)

    completed = subprocess.run(
        [root / "jobs/scripts/r2_transfer.sh", "pull-machine"],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_pull_app_refuses_to_overwrite_a_local_application_store(tmp_path: Path) -> None:
    # R2 is authoritative, but the local working copy may contain unpublished
    # judgments. Pull must not overwrite those changes merely because a remote
    # copy exists; they cannot be regenerated from market sources.
    bin_dir, log = _fake_aws(tmp_path)
    root = _fake_repo(tmp_path)
    app_store = root / "stores/application/baibai.sqlite"
    app_store.parent.mkdir(parents=True)
    app_store.write_bytes(b"local")
    env = _environment(bin_dir, log)
    generation = Path(env["R2_GENERATION_DIR"]) / "baibai.sqlite.etag"
    generation.write_text('"local-origin"\n')

    completed = subprocess.run(
        [root / "jobs/scripts/r2_transfer.sh", "pull-app"],
        cwd=root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "refusing application store download overwrite" in completed.stderr
    assert app_store.read_bytes() == b"local"
    assert generation.read_text() == '"local-origin"\n'
    assert not log.exists()


def test_only_the_application_pull_names_the_application_store() -> None:
    """No other pull command may reach `baibai.sqlite`.

    The guard inside `pull_app` is the second line of defence; the first is that no
    bulk command lists the application store at all. Asserting the dispatch source
    keeps that structural, because actually running a pull here would write to the
    operational store paths (the script resolves them from its own location).
    """
    script = TRANSFER_SCRIPT.read_text(encoding="utf-8")
    dispatch = script[script.index('case "${1:-}" in') :]
    pulls = {
        line.split(")")[0].strip()
        for line in dispatch.splitlines()
        if line.strip().startswith("pull-") and line.strip().endswith(")")
    }
    naming_app = {
        command
        for command in pulls
        if "baibai.sqlite" in dispatch.split(f"  {command})")[1].split(";;")[0]
        or command == "pull-app"
    }

    assert pulls == {
        "pull-app",
        "pull-indicators",
        "pull-machine",
        "pull-market",
        "pull-runs",
    }
    assert naming_app == {"pull-app"}


def test_pull_runs_is_scoped_to_the_cloud_authoritative_run_store() -> None:
    script = TRANSFER_SCRIPT.read_text(encoding="utf-8")
    dispatch = script[script.index('case "${1:-}" in') :]
    block = dispatch.split("  pull-runs)")[1].split(";;")[0]

    assert "pull_keys runs.sqlite" in block
    assert "market.sqlite" not in block
    assert "indicators.sqlite" not in block


@pytest.mark.parametrize("existing", ["runs.sqlite", "machine-manifest.json"])
def test_initial_seed_refuses_to_overwrite_an_existing_key(tmp_path: Path, existing: str) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["AWS_FAKE_EXISTING_KEY"] = existing

    completed = subprocess.run(
        [TRANSFER_SCRIPT, "seed-all"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert f"{existing} already exists" in completed.stderr
    assert not _remote_writes(log)


def test_initial_seed_uploads_all_stores_when_contract_keys_are_absent(tmp_path: Path) -> None:
    bin_dir, log = _fake_aws(tmp_path)

    env = _environment(bin_dir, log)
    env["TMPDIR"] = str(tmp_path)
    completed = subprocess.run(
        [TRANSFER_SCRIPT, "seed-all"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    commands = _transfer_commands(log)
    uploads = _remote_writes(log)
    assert [command.split("--key ", 1)[1].split()[0] for command in uploads] == [
        "market.sqlite",
        "runs.sqlite",
        "indicators.sqlite",
        "baibai.sqlite",
        "machine-manifest.json",
    ]
    assert all("--if-none-match *" in command for command in uploads)
    # Nothing is being replaced, so no generation is kept.
    assert all(".bak" not in command for command in commands)
    state = Path(env["AWS_FAKE_STATE"])
    receipt = _receipt(state)
    assert receipt is not None
    payload = json.loads(receipt)
    assert payload["schema_version"] == 1
    assert payload["keys"] == dict.fromkeys(("market.sqlite", "runs.sqlite"), "etag-put-1")
    assert "daily_publication" not in payload
    assert not list(tmp_path.glob("baibai-seed-receipt.*"))
    assert any(line.startswith("s3api get-object ") for line in commands)


def test_initial_seed_ignores_a_kept_generation_of_an_absent_store(tmp_path: Path) -> None:
    # A `.bak` left by an earlier push must not read as the store itself, or a
    # recovery seed after the real key was lost would be refused.
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["AWS_FAKE_EXISTING_KEY"] = "indicators.sqlite.bak"

    completed = subprocess.run(
        [TRANSFER_SCRIPT, "seed-all"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    commands = _transfer_commands(log)
    uploads = [command for command in commands if command.startswith("s3api put-object ")]
    assert len(uploads) == 5
    assert all("--if-none-match *" in command for command in uploads)


def test_seed_refuses_before_db_put_if_candidate_cannot_be_retained(tmp_path: Path) -> None:
    root = _fake_repo(tmp_path)
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["TMPDIR"] = str(tmp_path / "missing-receipt-directory")
    result = subprocess.run(
        [root / "jobs/scripts/r2_transfer.sh", "seed-all"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert not _remote_writes(log)


@pytest.mark.parametrize(
    ("injection", "key", "landed"),
    [
        ("AWS_FAKE_FAIL_PUT_KEY", "runs.sqlite", 1),
        ("AWS_FAKE_FAIL_PUT_KEY", "baibai.sqlite", 3),
        ("AWS_FAKE_LOST_PUT_ACK_KEY", "runs.sqlite", 2),
        ("AWS_FAKE_BAD_PUT_ETAG_KEY", "runs.sqlite", 2),
    ],
)
def test_seed_without_all_confirmed_bodies_never_creates_a_receipt_or_resumes(
    tmp_path: Path, injection: str, key: str, landed: int
) -> None:
    root = _fake_repo(tmp_path)
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["TMPDIR"] = str(tmp_path)
    script = root / "jobs/scripts/r2_transfer.sh"
    state = Path(env["AWS_FAKE_STATE"])

    initial = subprocess.run(
        [script, "seed-all"],
        cwd=root,
        env={**env, injection: key},
        capture_output=True,
        text=True,
        check=False,
    )
    assert initial.returncode != 0
    assert len(list(state.glob("put-*.sqlite"))) == landed
    assert not any("--key machine-manifest.json " in line for line in _remote_writes(log))
    assert not list(tmp_path.glob("baibai-seed-receipt.*"))

    log.write_text("")
    restart = subprocess.run(
        [script, "seed-all"], cwd=root, env=env, capture_output=True, text=True, check=False
    )
    assert restart.returncode == 2
    assert "already exists" in restart.stderr
    assert not _remote_writes(log)
    assert not list(root.glob(".r2-transfer.*"))


@pytest.mark.parametrize(
    "head_error",
    [
        "An error occurred (403) when calling the HeadObject operation: Forbidden",
        "Could not connect to the endpoint URL",
        None,  # The HEAD succeeds but returns a different ETag.
    ],
)
def test_seed_keeps_exact_acked_candidate_when_pair_cannot_be_verified(
    tmp_path: Path, head_error: str | None
) -> None:
    root = _fake_repo(tmp_path)
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["TMPDIR"] = str(tmp_path)
    script = root / "jobs/scripts/r2_transfer.sh"
    state = Path(env["AWS_FAKE_STATE"])
    fault = (
        {"AWS_FAKE_VERSION_HEAD_ERROR_KEY": "runs.sqlite", "AWS_FAKE_HEAD_ERROR": head_error}
        if head_error is not None
        else {"AWS_FAKE_CHANGED_KEY": "runs.sqlite"}
    )

    def run(*args: str | Path, **overrides: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [script, *args],
            cwd=root,
            env={**env, **overrides},
            capture_output=True,
            text=True,
            check=False,
        )

    interrupted = run("seed-all", **fault)
    assert interrupted.returncode == (2 if head_error is not None else 1)
    assert len(list(state.glob("put-*.sqlite"))) == 4
    assert len(_remote_writes(log)) == 4
    assert _receipt(state) is None
    (proof,) = tmp_path.glob("baibai-seed-receipt.*/machine-manifest.json")
    assert proof.stat().st_mode & 0o777 == 0o600
    assert proof.parent.stat().st_mode & 0o777 == 0o700
    assert list(proof.parent.iterdir()) == [proof]
    digest = hashlib.sha256(proof.read_bytes()).hexdigest()
    assert f"candidate_sha256={digest}" in interrupted.stderr
    assert json.loads(proof.read_bytes())["keys"] == dict.fromkeys(
        ("market.sqlite", "runs.sqlite"), "etag-put-1"
    )

    log.write_text("")
    assert run("seed-all").returncode == 2
    assert not _remote_writes(log)
    if head_error is None:
        # A retained candidate is proof of the ACKs, not permission to bless new heads.
        assert run("restore-machine-receipt", proof, digest, **fault).returncode == 1
        assert not _remote_writes(log)
        assert _receipt(state) is None
    else:
        restored = run("restore-machine-receipt", proof, digest)
        assert restored.returncode == 0, restored.stderr
        assert _receipt(state) == proof.read_text()
        (write,) = _remote_writes(log)
        assert "--key machine-manifest.json " in write
        assert "--if-none-match *" in write


@pytest.mark.parametrize("lost_ack", [False, True])
def test_seed_receipt_is_read_back_or_recoverable_without_reseeding(
    tmp_path: Path, lost_ack: bool
) -> None:
    root = _fake_repo(tmp_path)
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["TMPDIR"] = str(tmp_path)
    script = root / "jobs/scripts/r2_transfer.sh"
    state = Path(env["AWS_FAKE_STATE"])
    injection = "AWS_FAKE_LOST_PUT_ACK_KEY" if lost_ack else "AWS_FAKE_FAIL_PUT_KEY"

    initial = subprocess.run(
        [script, "seed-all"],
        cwd=root,
        env={**env, injection: "machine-manifest.json"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert len(_remote_writes(log)) == 5  # One PUT attempt per key even without a receipt ACK.
    if lost_ack:
        assert initial.returncode == 0, initial.stderr
        assert _receipt(state) is not None
        assert not list(tmp_path.glob("baibai-seed-receipt.*"))
    else:
        assert initial.returncode == 1, initial.stderr
        assert _receipt(state) is None
        (proof,) = tmp_path.glob("baibai-seed-receipt.*/machine-manifest.json")
        assert proof.stat().st_mode & 0o777 == 0o600
        digest = hashlib.sha256(proof.read_bytes()).hexdigest()
        assert f"candidate_sha256={digest}" in initial.stderr
        log.write_text("")
        restored = subprocess.run(
            [script, "restore-machine-receipt", proof, digest],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert restored.returncode == 0, restored.stderr
        assert _receipt(state) == proof.read_text()
        (write,) = _remote_writes(log)
        assert "--key machine-manifest.json " in write
        assert "--if-none-match *" in write
        assert not any(line.startswith("s3 cp ") for line in _transfer_commands(log))

    pulled = subprocess.run(
        [script, "pull-machine"], cwd=root, env=env, capture_output=True, text=True, check=False
    )
    assert pulled.returncode == 0, pulled.stderr
    assert "machine bundle receipt matches" in pulled.stdout


@pytest.mark.parametrize("github_actions", [False, True])
def test_machine_store_push_uploads_only_market_and_runs(
    tmp_path: Path, github_actions: bool
) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    if github_actions:
        env["GITHUB_ACTIONS"] = "true"

    completed = subprocess.run(
        [TRANSFER_SCRIPT, "push-machine"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    commands = _transfer_commands(log)
    uploads = [command for command in commands if command.startswith("s3api put-object ")]
    stores = [command for command in uploads if "--key machine-manifest.json" not in command]
    assert len(stores) == 2
    assert not any("indicators.sqlite" in command for command in commands)
    assert all('--if-match "etag-stable"' in command for command in stores)
    assert all(".bak" not in command for command in commands)
    # The receipt is written last and unconditionally: it describes the market/runs set the two
    # pushes just produced, so it cannot be bound to a generation any of them replaced.
    assert uploads[-1] == uploads[2]
    assert "--key machine-manifest.json" in uploads[-1]
    assert "--if-match" not in uploads[-1]


def _receipt(state: Path) -> str | None:
    written = state / "put-machine-manifest.json"
    return written.read_text(encoding="utf-8") if written.exists() else None


def _fixture_receipt(
    market: str = "etag-stable", runs: str = "etag-stable", **fields: object
) -> str:
    return json.dumps(
        {"schema_version": 1, "keys": {"market.sqlite": market, "runs.sqlite": runs}, **fields},
        indent=2,
    )


def _install_fixture_receipt(env: dict[str, str], receipt: str | None = None) -> None:
    key = "machine-manifest.json"
    env["AWS_FAKE_EXISTING_KEY"] = ":".join(filter(None, (env.get("AWS_FAKE_EXISTING_KEY"), key)))
    (Path(env["AWS_FAKE_STATE"]) / f"put-{key}").write_text(
        _fixture_receipt() if receipt is None else receipt
    )


def _pull_machine(tmp_path: Path, *, receipt: str | None) -> subprocess.CompletedProcess[str]:
    """Run a bundle pull in a throwaway root: the script resolves store paths from its
    own location, so running it from the checkout replaces the operational stores."""

    pull_root = tmp_path / "pull"
    pull_root.mkdir(exist_ok=True)
    bin_dir, log = _fake_aws(pull_root)
    root = _fake_repo(pull_root)
    env = _environment(bin_dir, log)
    if receipt is not None:
        _install_fixture_receipt(env, receipt)
    return subprocess.run(
        [root / "jobs/scripts/r2_transfer.sh", "pull-machine"],
        cwd=root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def test_a_push_that_stops_partway_leaves_no_receipt_for_the_set_it_made(
    tmp_path: Path,
) -> None:
    """Two store PUTs cannot be one commit, so a failed second PUT leaves a mixed set.

    No new receipt is written; the next pull refuses the previous set's receipt.
    """

    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["GITHUB_ACTIONS"] = "true"
    env["AWS_FAKE_FAIL_PUT_KEY"] = "runs.sqlite"

    completed = subprocess.run(
        [TRANSFER_SCRIPT, "push-machine"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    commands = _transfer_commands(log)
    pushed = [
        command
        for command in commands
        if command.startswith("s3api put-object ") and "--key machine-manifest.json" not in command
    ]
    # Market went out and runs failed; the independent economic store was untouched.
    assert len(pushed) == 2
    assert "--key market.sqlite" in pushed[0]
    assert "--key runs.sqlite" in pushed[1]
    assert not any("--key indicators.sqlite" in command for command in pushed)
    assert _receipt(Path(env["AWS_FAKE_STATE"])) is None


def test_a_bundle_pull_refuses_a_set_the_receipt_does_not_name(tmp_path: Path) -> None:
    """Per key the mixed set looks intact — the straddle check compares each object with
    itself — so only the receipt can tell the pull that these stores were never together."""

    stale = (
        '{\n  "schema_version": 1,\n  "written_at_utc": "2026-08-18T08:37:45Z",\n'
        '  "keys": {\n'
        '    "market.sqlite": "etag-before-the-failed-push",\n'
        '    "runs.sqlite": "etag-stable",\n'
        '    "indicators.sqlite": "etag-stable"\n  }\n}\n'
    )

    completed = _pull_machine(tmp_path, receipt=stale)

    assert completed.returncode == 1
    assert "R2 holds market.sqlite at generation etag-stable" in completed.stderr
    assert "receipt names etag-before-the-failed-push" in completed.stderr
    assert "wait for it to finish and pull again" in completed.stderr
    # Not "retry": a push that stopped partway is not cleared by running it again.
    assert "no retry clears this by itself" in completed.stderr
    assert "jobs/OPERATIONS.md" in completed.stderr
    # The refusal happens before anything is downloaded, so the local stores stand.
    assert not any(
        command.startswith("s3 cp s3://baibai-stores/")
        for command in _transfer_commands(tmp_path / "pull" / "aws.log")
    )


@pytest.mark.parametrize("indent", [None, 0, 2])
def test_a_bundle_pull_accepts_the_set_the_receipt_names(
    tmp_path: Path, indent: int | None
) -> None:
    current = json.dumps(
        {
            "schema_version": 1,
            "written_at_utc": "2026-08-19T00:00:00Z",
            "keys": {
                "market.sqlite": "etag-stable",
                "runs.sqlite": "etag-stable",
                "indicators.sqlite": "etag-stable",
            },
        },
        indent=indent,
    )

    completed = _pull_machine(tmp_path, receipt=current)

    assert completed.returncode == 0, completed.stderr
    assert "machine bundle receipt matches" in completed.stdout


def test_bundle_pull_checks_the_pair_not_the_lake_publication(tmp_path: Path) -> None:
    root = _fake_repo(tmp_path)
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    pointer = "lake/pointers/market/current.json"
    env["AWS_FAKE_EXISTING_KEY"] = pointer
    state = Path(env["AWS_FAKE_STATE"])
    (state / f"put-{pointer.replace('/', '_')}").write_text('{"release_id":"ahead"}')
    _install_fixture_receipt(
        env,
        _fixture_receipt(
            daily_publication={
                "run": "12345-1",
                "release_id": "previous",
                "release_manifest_sha256": "a" * 64,
            }
        ),
    )
    result = subprocess.run(
        [root / "jobs/scripts/r2_transfer.sh", "pull-machine"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "machine bundle receipt matches" in result.stdout
    assert all(pointer not in line for line in _transfer_commands(log))


@pytest.mark.parametrize("indicator_key", ["macro.sqlite", "indicators.sqlite"])
def test_machine_receipt_ignores_retired_economic_member(
    tmp_path: Path, indicator_key: str
) -> None:
    receipt = json.dumps(
        {
            "schema_version": 1,
            "keys": {
                "market.sqlite": "etag-stable",
                "runs.sqlite": "etag-stable",
                indicator_key: "etag-stable",
            },
        },
        indent=2,
    )
    result = _pull_machine(tmp_path, receipt=receipt)
    assert result.returncode == 0
    assert "machine bundle receipt matches" in result.stdout


@pytest.mark.parametrize(
    "receipt",
    [
        '{"keys": {"market.sqlite": ""}}',
        '{"keys": {"market.sqlite": 1}}',
        '{"keys": []}',
        "[]",
        '{"elsewhere": {"market.sqlite": "etag-stable"}, "keys": {}}',
        '{"keys": {"market.sqlite": "etag-stable"}',
        _fixture_receipt(schema_version=True),
        _fixture_receipt(schema_version=2),
    ],
)
def test_a_receipt_that_does_not_read_back_refuses_rather_than_passing(
    tmp_path: Path, receipt: str
) -> None:
    """A receipt this script cannot parse says nothing about the set, and nothing is
    exactly what a check that waves it through would also say."""

    completed = _pull_machine(tmp_path, receipt=receipt)

    assert completed.returncode == 1
    assert "receipt names no generation" in completed.stderr


RECOVERY_SECTION = "部分 push からの復旧"


def test_the_rejection_names_a_recovery_the_runbook_actually_carries() -> None:
    """The message is the only thing an operator has at the moment the pull stops.

    It used to say "re-dispatch the batch", which cannot work: the re-dispatch starts
    with the same pull and stops at the same place. Pointing at a section is only better
    while the section exists and carries the step it promises.
    """

    message = (REPO_ROOT / "jobs/scripts/r2_transfer.sh").read_text(encoding="utf-8")
    runbook = (REPO_ROOT / "jobs/OPERATIONS.md").read_text(encoding="utf-8")

    assert RECOVERY_SECTION in message
    assert "jobs/OPERATIONS.md" in message
    assert "re-dispatch" not in message
    assert f"### {RECOVERY_SECTION}" in runbook
    # The step the message sends the reader to: one exact key, never a prefix.
    assert "restore-machine-receipt" in runbook
    assert "delete-object --bucket baibai-stores --key machine-manifest.json" not in runbook


def test_a_retry_after_a_partial_push_is_refused_by_its_own_precheck(tmp_path: Path) -> None:
    """Recovery is not "run it again".

    Each conditional PUT is bound to the generation the failed run pulled, and the one
    that already succeeded changed that key. Running the command again — by hand or by
    re-dispatching the batch — is refused before it writes anything.
    """

    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["GITHUB_ACTIONS"] = "true"

    first = subprocess.run(
        [TRANSFER_SCRIPT, "push-machine"],
        cwd=REPO_ROOT,
        env={**env, "AWS_FAKE_FAIL_PUT_KEY": "runs.sqlite"},
        check=False,
        capture_output=True,
        text=True,
    )
    log.write_text("", encoding="utf-8")
    retry = subprocess.run(
        [TRANSFER_SCRIPT, "push-machine"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert first.returncode != 0
    assert retry.returncode != 0
    assert "market.sqlite changed on R2 after the pull" in retry.stderr
    # Refused before writing: the mixed set the first run left is not made worse.
    assert not [
        command
        for command in _transfer_commands(log)
        if command.startswith(("s3api put-object ", "s3api copy-object "))
    ]


@pytest.mark.parametrize(
    "case",
    ["daily", "unmarked", "other-attempt", "other-workflow", "failed-models", "other-local-lake"],
)
def test_only_a_matching_daily_success_can_tag_the_machine_receipt(tmp_path: Path, case: str):
    bin_dir, log = _fake_aws(tmp_path)
    root = _fake_repo(tmp_path)
    env = _environment(bin_dir, log)
    env.update(
        GITHUB_ACTIONS="true",
        GITHUB_REPOSITORY="owner/repo",
        GITHUB_REF="refs/heads/main",
        GITHUB_WORKFLOW_REF="owner/repo/.github/workflows/cloud-daily-batch-current.yml@refs/heads/main",
        GITHUB_RUN_ID="12345",
        GITHUB_RUN_ATTEMPT="1",
        SNAPSHOT_FAKE_COPY="1",
    )
    if case == "other-workflow":
        env["GITHUB_WORKFLOW_REF"] = "owner/repo/.github/workflows/other.yml@refs/heads/main"
    state = Path(env["AWS_FAKE_STATE"])
    market = root / "stores/market/market.sqlite"
    market.parent.mkdir(parents=True)
    with closing(sqlite3.connect(market)) as conn, conn:
        conn.execute(
            "CREATE TABLE lake_store_origin(singleton INTEGER, release_id TEXT, release_manifest_sha256 TEXT)"
        )
        conn.execute(
            "INSERT INTO lake_store_origin VALUES(1, ?, ?)",
            ("other" if case == "other-local-lake" else "published", "a" * 64),
        )
    for rel in ("screening/runs.sqlite", "market_context/indicators.sqlite"):
        path = root / "stores" / rel
        path.parent.mkdir(parents=True)
        path.write_bytes(b"fixed synthetic store")
    notice = tmp_path / "notice.json"
    lake = {"release_id": "published", "release_manifest_sha256": "a" * 64}
    notice.write_text(
        json.dumps(
            {
                "models_ready": case != "failed-models",
                "lake": lake,
                "daily_run": "12345-2" if case == "other-attempt" else "12345-1",
            }
        )
    )
    command = [root / "jobs/scripts/r2_transfer.sh", "push-machine"]
    if case != "unmarked":
        command.extend(["--daily-notice", str(notice)])
    completed = subprocess.run(
        command, cwd=root, env=env, check=False, capture_output=True, text=True
    )
    if case in {"daily", "unmarked"}:
        assert completed.returncode == 0, completed.stderr
        raw = (state / "put-machine-manifest.json").read_bytes()
        assert f"machine_receipt_sha256={hashlib.sha256(raw).hexdigest()}" in completed.stdout
        origin = json.loads(raw).get("daily_publication")
        assert origin == ({"run": "12345-1", **lake} if case == "daily" else None)
    else:
        assert completed.returncode != 0
        assert "machine_receipt_sha256=" not in completed.stdout
        assert not (state / "put-machine-manifest.json").exists()
        assert not log.exists() or "s3api put-object" not in log.read_text()


@pytest.mark.parametrize("prior", ["absent", "stale"])
@pytest.mark.parametrize("lost_ack", [False, True])
def test_exact_receipt_recovery_writes_only_the_receipt(
    tmp_path: Path, prior: str, lost_ack: bool
) -> None:
    root = _fake_repo(tmp_path)
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    state = Path(env["AWS_FAKE_STATE"])
    file = tmp_path / "verified-private-receipt.json"
    # The original receipt is restored byte-for-byte, including the daily origin.
    raw = (
        _fixture_receipt(
            daily_publication={
                "run": "12345-1",
                "release_id": "original",
                "release_manifest_sha256": "a" * 64,
            }
        )
        + "\n"
    )
    file.write_text(raw)
    digest = hashlib.sha256(file.read_bytes()).hexdigest()
    if prior == "stale":
        _install_fixture_receipt(env, _fixture_receipt(market="old-market"))
    if lost_ack:
        env["AWS_FAKE_LOST_PUT_ACK_KEY"] = "machine-manifest.json"
    for key, path in _machine_stores(root).items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"unpublished local")
        (state / f"put-{key}").write_text(f"remote {key}")

    def restore():
        return subprocess.run(
            [root / "jobs/scripts/r2_transfer.sh", "restore-machine-receipt", file, digest],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    result = restore()
    assert result.returncode == 0, result.stderr
    assert _receipt(state) == raw
    (write,) = _remote_writes(log)
    assert "--key machine-manifest.json " in write
    condition = '--if-match "etag-stable"' if prior == "stale" else "--if-none-match *"
    assert condition in write
    assert not any(line.startswith("s3 cp ") for line in _transfer_commands(log))
    assert all(path.read_bytes() == b"unpublished local" for path in _machine_stores(root).values())
    assert {p.read_text() for p in state.glob("put-*.sqlite")} == {
        "remote market.sqlite",
        "remote runs.sqlite",
    }

    log.write_text("")
    again = restore()
    assert again.returncode == 0, again.stderr
    assert "already verified; no write" in again.stdout
    assert not _remote_writes(log)


@pytest.mark.parametrize(
    ("fault", "diagnostic"),
    [
        ("wrong_sha", "proven SHA-256"),
        ("wrong_pair", "differ from the proven pair"),
        ("newer_receipt", "not provably stale"),
        ("unknown_schema", "not provably stale"),
        ("expired_lease", "publication lease"),
        ("lost_lease", "publication lease"),
        ("receipt_forbidden", "HeadObject failed"),
        ("absent_cas_race", "receipt write not verified"),
        ("stale_cas_race", "receipt write not verified"),
    ],
)
def test_exact_receipt_recovery_refuses_an_unproved_or_replaced_target(
    tmp_path: Path, fault: str, diagnostic: str
) -> None:
    root = _fake_repo(tmp_path)
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    state = Path(env["AWS_FAKE_STATE"])
    file = tmp_path / "retained-receipt.json"
    file.write_text(_fixture_receipt(runs="wrong" if fault == "wrong_pair" else "etag-stable"))
    digest = "0" * 64 if fault == "wrong_sha" else hashlib.sha256(file.read_bytes()).hexdigest()
    if fault == "newer_receipt":
        _install_fixture_receipt(env, _fixture_receipt(written_at_utc="newer"))
    if fault == "unknown_schema":
        _install_fixture_receipt(env, _fixture_receipt(schema_version=2))
    if fault == "stale_cas_race":
        _install_fixture_receipt(env, _fixture_receipt(market="old-market"))
    if fault == "expired_lease":
        Path(env["PUBLICATION_LEASE_HANDLE"]).write_text("synthetic-released")
    if fault == "lost_lease":
        _install_fixture_receipt(env, _fixture_receipt(market="old-market"))
        env["LEASE_FAKE_RELEASE_ON"] = (
            "get-object --bucket baibai-stores --key machine-manifest.json"
        )
    if fault == "receipt_forbidden":
        env.update(
            AWS_FAKE_HEAD_ERROR_KEY="machine-manifest.json",
            AWS_FAKE_HEAD_ERROR="An error occurred (403) when calling the HeadObject operation",
        )
    concurrent = _fixture_receipt(written_at_utc="concurrent")
    races = fault.endswith("_cas_race")
    if races:
        env["AWS_FAKE_CONCURRENT_RECEIPT_BODY"] = concurrent
    previous = _receipt(state)

    result = subprocess.run(
        [root / "jobs/scripts/r2_transfer.sh", "restore-machine-receipt", file, digest],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert diagnostic in result.stderr
    assert _receipt(state) == (concurrent if races else previous)
    assert len(_remote_writes(log)) == int(races)
    assert not list(root.glob(".r2-transfer.*"))


def test_machine_store_push_rejects_a_generation_replaced_by_manual_publish(
    tmp_path: Path,
) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["GITHUB_ACTIONS"] = "true"
    env["AWS_FAKE_ETAG_OVERRIDE"] = '"etag-after-manual-publish"'

    completed = subprocess.run(
        [TRANSFER_SCRIPT, "push-machine"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "market.sqlite changed on R2 after the pull" in completed.stderr
    commands = _transfer_commands(log)
    assert not any(command.startswith("s3api copy-object ") for command in commands)
    assert not any(command.startswith("s3api put-object ") for command in commands)


def test_machine_store_push_reports_where_each_key_spends_its_time(tmp_path: Path) -> None:
    """Separate the ordinary machine PUT call from the preceding lease check."""
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["GITHUB_ACTIONS"] = "true"

    result = subprocess.run(
        [TRANSFER_SCRIPT, "push-machine"],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    reported = dict(
        re.findall(
            r"^store push: key=(\S+) bytes=(\d+) snapshot=\d+s backup=\d+s upload=\d+s"
            r" lease_before_put=\d+s conditional_put_cli=\d+s$",
            result.stdout,
            flags=re.MULTILINE,
        )
    )
    # The staged snapshot is what gets uploaded, so its size is the transferred byte
    # count. The fake snapshot writes one byte.
    assert reported == {"market.sqlite": "1", "runs.sqlite": "1"}


def _push_app(tmp_path: Path) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["AWS_FAKE_EXISTING_KEY"] = "baibai.sqlite"
    (Path(env["R2_GENERATION_DIR"]) / "baibai.sqlite.etag").write_text(
        '"etag-stable"\n', encoding="utf-8"
    )
    completed = subprocess.run(
        [TRANSFER_SCRIPT, "push-app"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    return completed, _transfer_commands(log)


@pytest.mark.parametrize("failure", ["unknown", "empty", "changed", "put-race"])
def test_application_push_refuses_unknown_or_changed_generation(
    tmp_path: Path, failure: str
) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["AWS_FAKE_EXISTING_KEY"] = "baibai.sqlite"
    if failure != "unknown":
        (Path(env["R2_GENERATION_DIR"]) / "baibai.sqlite.etag").write_text(
            '"etag-stable"\n', encoding="utf-8"
        )
    if failure == "empty":
        (Path(env["R2_GENERATION_DIR"]) / "baibai.sqlite.etag").write_text("")
    if failure == "changed":
        env["AWS_FAKE_ETAG_OVERRIDE"] = '"newer-cloud-generation"'
    if failure == "put-race":
        env["AWS_FAKE_REJECT_CONDITIONAL_PUT"] = "1"
    result = subprocess.run(
        [TRANSFER_SCRIPT, "push-app"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    commands = _transfer_commands(log)
    assert not any(command.startswith("s3 cp ") for command in commands)
    assert not any("delete-object" in command for command in commands)
    assert not (Path(env["AWS_FAKE_STATE"]) / "generation-baibai.sqlite").exists()
    if failure == "put-race":
        assert "PreconditionFailed" in result.stderr
        assert any('--if-match "etag-stable"' in command for command in commands)
    else:
        assert not any("put-object" in command or "copy-object" in command for command in commands)


def test_application_push_records_its_own_put_generation(tmp_path: Path) -> None:
    result, commands = _push_app(tmp_path)
    assert result.returncode == 0, result.stderr
    assert [command.split()[:2] for command in commands] == [
        ["s3api", "head-object"],
        ["s3api", "put-object"],
    ]
    head, put = commands
    assert "--key baibai.sqlite " in head
    assert "--key baibai.sqlite " in put
    assert '--if-match "etag-stable"' in put
    assert (tmp_path / "r2-generations/baibai.sqlite.etag").read_text() == '"etag-put-1"\n'


def test_application_can_publish_again_using_its_put_response(tmp_path: Path) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["AWS_FAKE_EXISTING_KEY"] = "baibai.sqlite"
    generation = Path(env["R2_GENERATION_DIR"]) / "baibai.sqlite.etag"
    generation.write_text('"etag-stable"\n')
    for expected in ('"etag-put-1"', '"etag-put-2"'):
        result = subprocess.run(
            [TRANSFER_SCRIPT, "push-app"],
            cwd=REPO_ROOT,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert generation.read_text().strip() == expected
    puts = [line for line in _transfer_commands(log) if line.startswith("s3api put-object ")]
    assert '--if-match "etag-put-1"' in puts[1]


def test_application_put_without_etag_removes_stale_marker(tmp_path: Path) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["AWS_FAKE_MISSING_PUT_ETAG"] = "1"
    generation = Path(env["R2_GENERATION_DIR"]) / "baibai.sqlite.etag"
    generation.write_text('"etag-stable"\n')
    result = subprocess.run(
        [TRANSFER_SCRIPT, "push-app"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "application uploaded but no ETag returned" in result.stderr
    assert not generation.exists()
    assert (Path(env["AWS_FAKE_STATE"]) / "generation-baibai.sqlite").exists()


def test_machine_store_push_keeps_one_generation_of_the_store_it_replaces(
    tmp_path: Path,
) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["GITHUB_ACTIONS"] = "true"
    env["AWS_FAKE_EXISTING_KEY"] = "runs.sqlite"

    completed = subprocess.run(
        [TRANSFER_SCRIPT, "push-machine"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    commands = _transfer_commands(log)
    backups = [index for index, command in enumerate(commands) if ".bak" in command]
    uploads = [
        index
        for index, command in enumerate(commands)
        if command.startswith("s3api put-object ") and "--key runs.sqlite" in command
    ]
    assert len(backups) == 1
    assert len(uploads) == 1
    # The generation is kept from the remote object before it is overwritten, through
    # CopyObject: `aws s3 cp` picks its S3-to-S3 implementation by object size and both
    # branches ask R2 for tagging operations it does not implement.
    assert commands[backups[0]].startswith("s3api copy-object ")
    assert "--key runs.sqlite.bak " in f"{commands[backups[0]]} "
    assert "--copy-source baibai-stores/runs.sqlite" in commands[backups[0]]
    assert '--copy-source-if-match "etag-stable"' in commands[backups[0]]
    assert backups[0] < uploads[0]


def test_store_backup_waits_past_the_cli_default_read_timeout(tmp_path: Path) -> None:
    # R2 answers CopyObject only once the copy is finished, and a several-hundred-MB store
    # outlasts the CLI's 60s default read timeout. Without a wider ceiling the kept
    # generation times out and takes the whole machine-store push down with it.
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["GITHUB_ACTIONS"] = "true"
    env["AWS_FAKE_EXISTING_KEY"] = "market.sqlite"

    completed = subprocess.run(
        [TRANSFER_SCRIPT, "push-machine"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    commands = _transfer_commands(log)
    backup = next(command for command in commands if command.startswith("s3api copy-object "))
    arguments = backup.split()
    assert int(arguments[arguments.index("--cli-read-timeout") + 1]) > 60


def test_indicator_push_merges_the_cloud_store_before_uploading(tmp_path: Path) -> None:
    # Runs outside GitHub Actions on purpose: the deep history this publishes is fetched
    # locally, and the merge — not the caller's environment — is what guarantees the cloud
    # store is not rolled back.
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["AWS_FAKE_EXISTING_KEY"] = "indicators.sqlite:indicators.sqlite.bak"

    completed = subprocess.run(
        [TRANSFER_SCRIPT, "push-indicators"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    commands = _transfer_commands(log)
    downloads = [
        index
        for index, command in enumerate(commands)
        if command.startswith("s3 cp s3://baibai-stores/indicators.sqlite ")
    ]
    merges = [index for index, command in enumerate(commands) if command.startswith("merge ")]
    uploads = [
        index
        for index, command in enumerate(commands)
        if command.startswith("s3api put-object ") and "--key indicators.sqlite" in command
    ]
    assert len(downloads) == 1
    assert len(merges) == 1
    assert len(uploads) == 1
    assert downloads[0] < merges[0] < uploads[0]
    assert "--target" in commands[merges[0]]
    assert "--canonical" in commands[merges[0]]
    assert "--source" not in commands[merges[0]]
    assert "stores/market_context/indicators.sqlite" in commands[merges[0]]
    assert '--if-match "etag-stable"' in commands[uploads[0]]
    assert _remote_writes(log) == [commands[uploads[0]]]
    assert not any("indicators.sqlite.bak" in command for command in commands)
    assert "backup=" not in completed.stdout


@pytest.mark.parametrize("failure", ["lease", "generation"])
def test_indicator_push_checks_lease_and_generation_after_merging(tmp_path, failure):
    root = _fake_repo(tmp_path)
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["AWS_FAKE_EXISTING_KEY"] = "indicators.sqlite"
    if failure == "lease":
        env["LEASE_FAKE_RELEASE_ON"] = "s3 cp s3://baibai-stores/indicators.sqlite "
    else:
        env["AWS_FAKE_CHANGED_KEY"] = "indicators.sqlite"

    completed = subprocess.run(
        [root / "jobs/scripts/r2_transfer.sh", "push-indicators"],
        cwd=root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    if failure == "lease":
        assert "publication lease" in completed.stderr
    else:
        assert "indicators.sqlite changed on R2" in completed.stderr
    assert any(command.startswith("merge ") for command in _transfer_commands(log))
    assert _remote_writes(log) == []


def test_indicator_push_uploads_nothing_when_the_merge_refuses(tmp_path: Path) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["MERGE_FAKE_EXIT"] = "1"

    completed = subprocess.run(
        [TRANSFER_SCRIPT, "push-indicators"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    commands = _transfer_commands(log)
    assert any(command.startswith("merge ") for command in commands)
    assert not any(
        command.startswith("s3api put-object ") and "--key indicators.sqlite" in command
        for command in commands
    )


def test_market_push_merges_the_cloud_store_before_uploading(tmp_path: Path) -> None:
    # Runs outside GitHub Actions on purpose. The deep history this publishes is fetched
    # where there is time for it, and the merge — not the caller's environment — is what
    # keeps the daily batch's recent rows from being rolled back.
    bin_dir, log = _fake_aws(tmp_path)

    completed = subprocess.run(
        [TRANSFER_SCRIPT, "push-market"],
        cwd=REPO_ROOT,
        env=_environment(bin_dir, log),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    commands = _transfer_commands(log)
    downloads = [
        index
        for index, command in enumerate(commands)
        if command.startswith("s3 cp s3://baibai-stores/market.sqlite ")
    ]
    merges = [index for index, command in enumerate(commands) if command.startswith("merge ")]
    uploads = [
        index
        for index, command in enumerate(commands)
        if command.startswith("s3api put-object ") and "--key market.sqlite" in command
    ]
    assert len(downloads) == 1
    assert len(merges) == 1
    assert len(uploads) == 1
    assert downloads[0] < merges[0] < uploads[0]
    assert "stores/market/market.sqlite" in commands[merges[0]]
    assert '--if-match "etag-stable"' in commands[uploads[0]]
    # The receipt reads the run-store generation, but only market rows are uploaded.
    writes = [
        command
        for command in commands
        if command.startswith(("s3api put-object ", "s3api copy-object ", "s3 cp "))
    ]
    assert all("runs.sqlite" not in command for command in writes)
    assert all("indicators.sqlite" not in command for command in writes)


def test_market_dehydrate_resolves_current_and_leaves_identity_check_to_the_core(
    tmp_path: Path,
) -> None:
    root = _fake_repo(tmp_path)
    market = root / "stores/market/market.sqlite"
    market.parent.mkdir(parents=True)
    connection = open_connection(market)
    write_lake_store_origin(
        connection,
        LakeStoreOrigin(
            release_id="sqlite-release",
            release_manifest_sha256="c" * 64,
        ),
    )
    connection.commit()
    connection.close()
    bin_dir, log = _fake_aws(tmp_path)
    environment = _environment(bin_dir, log)
    environment["SNAPSHOT_FAKE_COPY"] = "1"
    snapshot_calls = tmp_path / "snapshot-calls"
    environment["SNAPSHOT_LOG"] = str(snapshot_calls)
    environment["AWS_FAKE_EXISTING_KEY"] = "lake/pointers/market/current.json"

    completed = subprocess.run(
        [root / "jobs/scripts/r2_transfer.sh", "push-market"],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    dehydrate = next(
        command
        for command in log.read_text(encoding="utf-8").splitlines()
        if command.startswith("dehydrate ")
    )
    assert "--source " in dehydrate
    assert "--store " in dehydrate
    calls = snapshot_calls.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 1
    assert "baibai_engine.storage.sqlite.snapshot check --path " in calls[0]
    assert "--release" not in dehydrate
    assert "--manifest-sha256" not in dehydrate


def test_market_push_uploads_nothing_when_the_merge_refuses(tmp_path: Path) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["MERGE_FAKE_EXIT"] = "1"

    completed = subprocess.run(
        [TRANSFER_SCRIPT, "push-market"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    commands = _transfer_commands(log)
    assert not any(
        command.startswith("s3api put-object ") and "--key market.sqlite" in command
        for command in commands
    )


def test_market_push_stops_before_backup_when_cloud_changes_during_merge(tmp_path: Path) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["AWS_FAKE_CHANGED_KEY"] = "market.sqlite"
    env["AWS_FAKE_EXISTING_KEY"] = "market.sqlite"

    completed = subprocess.run(
        [TRANSFER_SCRIPT, "push-market"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "market.sqlite changed on R2 during the merge" in completed.stderr
    commands = _transfer_commands(log)
    assert not any(command.startswith("s3api copy-object ") for command in commands)
    assert not any(command.startswith("s3api put-object ") for command in commands)


def test_market_push_condition_rejects_a_race_after_the_last_version_read(
    tmp_path: Path,
) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["AWS_FAKE_REJECT_CONDITIONAL_PUT"] = "1"

    completed = subprocess.run(
        [TRANSFER_SCRIPT, "push-market"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    commands = _transfer_commands(log)
    upload = next(command for command in commands if command.startswith("s3api put-object "))
    assert "--key market.sqlite" in upload
    assert '--if-match "etag-stable"' in upload
    assert not any(
        command.startswith("s3 cp ") and "s3://baibai-stores/market.sqlite" in command
        for command in commands
        if not command.startswith("s3 cp s3://")
    )


def _machine_stores(root: Path) -> dict[str, Path]:
    return {
        "market.sqlite": root / "stores/market/market.sqlite",
        "runs.sqlite": root / "stores/screening/runs.sqlite",
    }


def test_pull_machine_refuses_a_snapshot_that_straddles_a_push(tmp_path: Path) -> None:
    """A batch that pushes partway through the pull would leave mixed generations.

    Each store passes `check_sqlite` on its own, so nothing downstream notices that
    the run store knows a screening run the market store has no bars for.
    """

    bin_dir, log = _fake_aws(tmp_path)
    root = _fake_repo(tmp_path)
    for path in _machine_stores(root).values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"local")
    environment = _environment(bin_dir, log)
    environment["AWS_FAKE_CHANGED_KEY"] = "runs.sqlite"
    _install_fixture_receipt(environment, _fixture_receipt(runs="etag-1"))

    completed = subprocess.run(
        [root / "jobs/scripts/r2_transfer.sh", "pull-machine"],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert "runs.sqlite changed on R2 during the pull" in completed.stderr
    # No store is replaced, so the local set stays one consistent generation.
    assert all(path.read_bytes() == b"local" for path in _machine_stores(root).values())
    generation_dir = Path(environment["R2_GENERATION_DIR"])
    assert not (generation_dir / "market.sqlite.etag").exists()
    assert not (generation_dir / "runs.sqlite.etag").exists()
    assert (generation_dir / "indicators.sqlite.etag").read_text().strip() == '"etag-stable"'
    assert not list(root.glob(".r2-transfer.*"))
    commands = _transfer_commands(log)
    versions = [
        index
        for index, line in enumerate(commands)
        if line.startswith("s3api head-object ") and "--query ETag" in line
    ]
    downloads = [index for index, line in enumerate(commands) if line.startswith("s3 cp s3://")]
    # Every store's version is read before any download and re-read after all of
    # them; a straddle is only visible from both sides.
    assert len(downloads) == 2
    assert versions[:2] == [0, 1]
    assert min(versions[2:]) > max(downloads)


def test_pull_machine_replaces_every_store_when_no_push_intervened(tmp_path: Path) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    root = _fake_repo(tmp_path)
    for path in _machine_stores(root).values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"local")

    environment = _environment(bin_dir, log)
    _install_fixture_receipt(environment)
    completed = subprocess.run(
        [root / "jobs/scripts/r2_transfer.sh", "pull-machine"],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert all(path.read_bytes() == b"x" for path in _machine_stores(root).values())
    generation_dir = Path(environment["R2_GENERATION_DIR"])
    assert {
        path.name: path.read_text(encoding="utf-8") for path in generation_dir.glob("*.etag")
    } == {
        "market.sqlite.etag": '"etag-stable"\n',
        "runs.sqlite.etag": '"etag-stable"\n',
        "indicators.sqlite.etag": '"etag-stable"\n',
    }
    assert not list(root.glob(".r2-transfer.*"))


@pytest.mark.parametrize("status", [404, 403])
def test_pull_machine_without_a_readable_receipt_preserves_local_store_bytes(
    tmp_path: Path, status: int
) -> None:
    root = _fake_repo(tmp_path)
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    if status == 403:
        env.update(
            AWS_FAKE_HEAD_ERROR_KEY="machine-manifest.json",
            AWS_FAKE_HEAD_ERROR="An error occurred (403) when calling the HeadObject operation",
        )
    for path in _machine_stores(root).values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"unpublished local")
    result = subprocess.run(
        [root / "jobs/scripts/r2_transfer.sh", "pull-machine"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == (1 if status == 404 else 2)
    assert ("receipt is missing" in result.stderr) == (status == 404)
    assert all(path.read_bytes() == b"unpublished local" for path in _machine_stores(root).values())
    assert {file.name for file in Path(env["R2_GENERATION_DIR"]).iterdir()} == {
        "indicators.sqlite.etag"
    }
    assert not any(line.startswith("s3 cp ") for line in _transfer_commands(log))
    assert not _remote_writes(log)
    assert not list(root.glob(".r2-transfer.*"))


def test_the_transfer_script_names_the_same_pointer_key_the_engine_publishes() -> None:
    """The emptying gate reads this key; a rename would turn it off in silence.

    `_push_keys` empties the market copy only when the lake serves a release, and it
    decides that by looking for the pointer. A key that stopped matching would not
    error — it would simply never find a pointer, and every push would go back to
    carrying the full store while still reporting success.
    """

    from baibai_engine.storage.snapshots.keys import (
        current_release_pointer_key as lake_current_release_pointer_key,
    )

    assert lake_current_release_pointer_key() in TRANSFER_SCRIPT.read_text(encoding="utf-8")


def test_first_publication_needs_no_sidecar_and_prints_its_result(
    tmp_path: Path,
) -> None:
    """The canonical shell path supports first publication without duplicate state."""

    root = _fake_repo(tmp_path)
    bin_dir, log = _fake_aws(tmp_path)
    environment = _environment(bin_dir, log)
    environment["PUBLICATION_LEASE_HANDLE"] = str(tmp_path / "lease.json")
    record = Path(environment["R2_GENERATION_DIR"]) / "lake-release.json"

    completed = subprocess.run(
        [root / "jobs/scripts/r2_transfer.sh", "publish-lake"],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    published = [line for line in log.read_text(encoding="utf-8").splitlines() if "publish" in line]
    assert len(published) == 1
    assert "--lease-handle" in published[0]
    assert str(tmp_path / "lease.json") in published[0]
    assert "--full-rebuild" not in published[0]
    assert "--base-release" not in published[0]
    assert "--origin-release" not in published[0]
    assert "--origin-manifest-sha256" not in published[0]
    assert "release-after-rebuild" in completed.stdout
    assert not record.exists()


@pytest.mark.parametrize("partial_stdout", ["", '{"release_id":"partial"'])
def test_a_failed_publish_does_not_touch_a_legacy_sidecar(
    tmp_path: Path, partial_stdout: str
) -> None:
    root = _fake_repo(tmp_path)
    bin_dir, log = _fake_aws(tmp_path)
    environment = _environment(bin_dir, log)
    environment["PUBLICATION_LEASE_HANDLE"] = str(tmp_path / "lease.json")
    environment["PUBLISH_FAKE_EXIT"] = "1"
    environment["PUBLISH_FAKE_PARTIAL"] = partial_stdout
    record = Path(environment["R2_GENERATION_DIR"]) / "lake-release.json"
    before = b'{"release_id": "release-before", "release_manifest_sha256": "old"}\n'
    record.write_bytes(before)

    completed = subprocess.run(
        [root / "jobs/scripts/r2_transfer.sh", "publish-lake"],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert record.read_bytes() == before


@pytest.mark.parametrize(
    "args",
    [
        (),
        ("unknown-command",),
        ("pull-app", "extra"),
        ("pull-machine", "extra"),
        ("pull-market", "extra"),
        ("pull-runs", "extra"),
        ("pull-indicators", "extra"),
        ("hydrate-market", "--release", "historical"),
        ("hydrate-market-for-daily", "extra"),
        ("hydrate-portfolio", "extra"),
        ("seed-all", "extra"),
        ("publish-lake", "incremental-please"),
        ("push-market", "extra"),
        ("push-runs", "extra"),
        ("push-app", "extra"),
        ("push-machine", "--daily-notice"),
        ("push-machine", "--evidence-dir", "--daily-notice"),
        ("push-machine", "--daily-notice", "notice.json", "extra"),
        ("push-indicators", "--recheck-source"),
        ("push-indicators", "--recheck-source", "--unknown-option"),
        ("push-indicators", "--unknown-option", "fred-payems"),
        ("upload-portfolio-views", ""),
        ("upload-portfolio-views", "--daily-notice"),
        ("upload-portfolio-views", "views", "extra"),
    ],
)
def test_invalid_transfer_arguments_stop_before_helpers_or_credentials(
    tmp_path: Path, args: tuple[str, ...]
) -> None:
    root = _fake_repo(tmp_path)
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    for name in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        env.pop(name)
    (root / ".env").write_text('printf "credentials loaded\n" >> "$AWS_LOG"\n', encoding="utf-8")
    uv = bin_dir / "uv"
    uv.write_text(
        '#!/bin/bash\nprintf "helper called\n" >> "$AWS_LOG"\nexit 11\n', encoding="utf-8"
    )

    completed = subprocess.run(
        [root / "jobs/scripts/r2_transfer.sh", *args],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "usage:" in completed.stderr
    assert not log.exists()


@pytest.mark.parametrize(
    ("action", "key"),
    [
        ("pull-machine", "machine-manifest.json"),
        ("push-machine", "lake/pointers/market/current.json"),
        ("seed-all", "market.sqlite"),
        ("seed-all", "machine-manifest.json"),
    ],
)
@pytest.mark.parametrize(
    "error",
    [
        "An error occurred (403) when calling the HeadObject operation: Forbidden",
        "An error occurred (500) when calling the HeadObject operation: InternalError",
        "Could not connect to the endpoint URL",
    ],
)
def test_head_failure_is_not_treated_as_absence(
    tmp_path: Path, action: str, key: str, error: str
) -> None:
    root = _fake_repo(tmp_path)
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["AWS_FAKE_HEAD_ERROR_KEY"] = key
    # CLI stderr can carry an endpoint, signed request or response text. None is a
    # public diagnostic, even though the failed existence check must still stop.
    private_detail = "https://synthetic-private.invalid/?token=synthetic-private-token"
    env["AWS_FAKE_HEAD_ERROR"] = f"{error}: {private_detail}"
    (Path(env["R2_GENERATION_DIR"]) / "baibai.sqlite.etag").write_text('"etag-stable"\n')

    result = subprocess.run(
        [root / "jobs/scripts/r2_transfer.sh", action],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert f"cannot determine whether R2 object {key} exists" in result.stderr
    assert "HeadObject failed; details withheld" in result.stderr
    assert error not in result.stdout + result.stderr
    assert private_detail not in result.stdout + result.stderr
    commands = _transfer_commands(log)
    assert not any(
        command.startswith(
            ("s3 cp ", "s3api put-object ", "s3api copy-object ", "s3api delete-object ")
        )
        for command in commands
    )
    assert not list(root.glob(".r2-transfer.*"))


@pytest.mark.parametrize("version", ['"synthetic-private/value"', '"synthetic-private\nvalue"'])
def test_malformed_remote_version_is_not_echoed_or_installed(tmp_path: Path, version: str) -> None:
    root = _fake_repo(tmp_path)
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    _install_fixture_receipt(env)
    env["AWS_FAKE_ETAG_OVERRIDE"] = version
    for path in _machine_stores(root).values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"local-store")

    result = subprocess.run(
        [root / "jobs/scripts/r2_transfer.sh", "pull-machine"],
        cwd=root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "unusable object version" in result.stderr
    assert "synthetic-private" not in result.stdout + result.stderr
    assert all(path.read_bytes() == b"local-store" for path in _machine_stores(root).values())
    commands = _transfer_commands(log)
    assert not any(
        command.startswith(
            ("s3 cp ", "s3api put-object ", "s3api copy-object ", "s3api delete-object ")
        )
        for command in commands
    )
    generation_dir = Path(env["R2_GENERATION_DIR"])
    assert not (generation_dir / "market.sqlite.etag").exists()
    assert not (generation_dir / "runs.sqlite.etag").exists()
    assert not list(root.glob(".r2-transfer.*"))


@pytest.mark.parametrize("fail_receipt", [False, True])
def test_machine_receipt_temporary_file_is_cleaned_on_success_or_failure(
    tmp_path: Path, fail_receipt: bool
) -> None:
    root = _fake_repo(tmp_path)
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    env["TMPDIR"] = str(scratch)
    if fail_receipt:
        env["AWS_FAKE_FAIL_PUT_KEY"] = "machine-manifest.json"

    result = subprocess.run(
        [root / "jobs/scripts/r2_transfer.sh", "push-machine"],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert (result.returncode != 0) == fail_receipt
    assert any("--key machine-manifest.json" in line for line in _transfer_commands(log))
    assert not list(root.glob(".r2-transfer.*"))
    assert not list(scratch.iterdir())


def test_initial_seed_refuses_a_race_after_the_absence_check(tmp_path: Path) -> None:
    root = _fake_repo(tmp_path)
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["AWS_FAKE_REJECT_CONDITIONAL_PUT"] = "1"
    remote = Path(env["AWS_FAKE_STATE"]) / "put-market.sqlite"
    remote.write_bytes(b"created by another writer after preflight")

    result = subprocess.run(
        [root / "jobs/scripts/r2_transfer.sh", "seed-all"],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "PreconditionFailed" in result.stderr
    commands = _transfer_commands(log)
    uploads = [command for command in commands if command.startswith("s3api put-object ")]
    assert len(uploads) == 1
    assert "--if-none-match *" in uploads[0]
    assert not any(command.startswith(("s3 cp ", "s3api copy-object ")) for command in commands)
    assert remote.read_bytes() == b"created by another writer after preflight"
    assert not list(root.glob(".r2-transfer.*"))


def test_runs_only_push_uses_pulled_generation_and_does_not_reupload_other_stores(tmp_path):
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    completed = subprocess.run(
        [TRANSFER_SCRIPT, "push-runs"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert "conditional_put_cli=" not in completed.stdout
    uploads = [
        command for command in _transfer_commands(log) if command.startswith("s3api put-object ")
    ]
    assert len(uploads) == 2
    assert "--key runs.sqlite " in uploads[0]
    assert '--if-match "etag-stable"' in uploads[0]
    assert "--key machine-manifest.json " in uploads[1]
    assert not any(
        "--key market.sqlite " in command or "--key indicators.sqlite " in command
        for command in uploads
    )


def test_runs_only_push_refuses_changed_cloud_generation_without_writing(tmp_path):
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["AWS_FAKE_CHANGED_KEY"] = "runs.sqlite"
    completed = subprocess.run(
        [TRANSFER_SCRIPT, "push-runs"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode != 0
    assert "runs.sqlite changed on R2" in completed.stderr
    assert not any(command.startswith("s3api put-object ") for command in _transfer_commands(log))


def _remote_writes(log):
    return [
        command
        for command in _transfer_commands(log)
        if command.startswith(("s3api put-object ", "s3api copy-object "))
        or (command.startswith("s3 cp ") and command.split()[3].startswith("s3://"))
    ]


@pytest.mark.parametrize(
    "operation",
    ["push-machine", "push-runs", "push-market", "push-indicators", "upload-portfolio-views"],
)
@pytest.mark.parametrize("state", ["unconnected", "missing", "rejected"])
def test_shared_transfer_refuses_unavailable_lease_before_any_remote_write(
    tmp_path, operation, state
):
    root = _fake_repo(tmp_path)
    binary, log = _fake_aws(tmp_path)
    env = _environment(binary, log)
    handle = Path(env["PUBLICATION_LEASE_HANDLE"])
    if state == "unconnected":
        env.pop("PUBLICATION_LEASE_HANDLE")
    elif state == "missing":
        handle.unlink()
    else:
        handle.write_text("synthetic-rejected")
    # This stub models a rejected verify command, not real expiry or release states.
    # Those belong to test_publication_lease's verify CLI tests.
    args = [root / "jobs/scripts/r2_transfer.sh", operation]
    if operation == "upload-portfolio-views":
        env["GITHUB_ACTIONS"] = "true"
        args.append(tmp_path / "serving")
    result = subprocess.run(args, env=env, cwd=root, capture_output=True, text=True)
    assert result.returncode != 0
    assert "publication lease" in result.stderr
    assert _remote_writes(log) == []


@pytest.mark.parametrize(
    ("operation", "release_on", "expected_key"),
    [
        (
            "push-machine",
            "s3api put-object --bucket baibai-stores --key market.sqlite ",
            "market.sqlite",
        ),
        ("push-runs", "s3api put-object --bucket baibai-stores --key runs.sqlite ", "runs.sqlite"),
        (
            "push-market",
            "s3api put-object --bucket baibai-stores --key market.sqlite ",
            "market.sqlite",
        ),
        (
            "upload-portfolio-views",
            "s3://baibai-serving/views/portfolio-dashboard.json",
            "views/portfolio-dashboard.json",
        ),
    ],
)
def test_shared_transfer_stops_the_next_write_after_release(
    tmp_path, operation, release_on, expected_key
):
    root = _fake_repo(tmp_path)
    binary, log = _fake_aws(tmp_path)
    env = _environment(binary, log)
    env["LEASE_FAKE_RELEASE_ON"] = release_on
    args = [root / "jobs/scripts/r2_transfer.sh", operation]
    if operation == "upload-portfolio-views":
        env["GITHUB_ACTIONS"] = "true"
        views = tmp_path / "serving/views"
        views.mkdir(parents=True)
        for name in ("portfolio-dashboard", "outcomes", "latest-proposal", "proposal-history"):
            (views / f"{name}.json").write_text('{"synthetic": true}')
        args.append(views.parent)
    result = subprocess.run(args, env=env, cwd=root, capture_output=True, text=True)
    assert result.returncode != 0
    assert "publication lease" in result.stderr
    writes = _remote_writes(log)
    assert len(writes) == 1
    assert expected_key in writes[0]
    if operation in {"push-machine", "push-runs", "push-market"}:
        assert '--if-match "etag-stable"' in writes[0]
    assert "machine-manifest.json" not in "\n".join(writes)
    assert not list(root.glob(".r2-transfer.*"))


@pytest.mark.parametrize("operation", ["seed-all", "push-app"])
def test_application_and_initial_seed_do_not_require_the_common_lease(tmp_path, operation):
    root = _fake_repo(tmp_path)
    binary, log = _fake_aws(tmp_path)
    env = _environment(binary, log)
    env.pop("PUBLICATION_LEASE_HANDLE")
    (Path(env["R2_GENERATION_DIR"]) / "baibai.sqlite.etag").write_text('"etag-stable"\n')
    result = subprocess.run(
        [root / "jobs/scripts/r2_transfer.sh", operation],
        env=env,
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert len(_remote_writes(log)) == (5 if operation == "seed-all" else 1)
    assert not (Path(env["AWS_FAKE_STATE"]) / "lease-checks").exists()


def test_transfer_lease_verifies_the_selected_stores_bucket_not_a_late_env_override(tmp_path):
    root = _fake_repo(tmp_path)
    binary, log = _fake_aws(tmp_path)
    env = _environment(binary, log)
    env["R2_STORES_BUCKET"] = "selected-stores"
    env.pop("R2_ACCESS_KEY_ID")
    (root / ".env").write_text(
        "R2_ACCESS_KEY_ID=synthetic-local-key\nR2_STORES_BUCKET=late-local-override\n"
    )
    result = subprocess.run(
        [root / "jobs/scripts/r2_transfer.sh", "push-runs"],
        env=env,
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    checks = (Path(env["AWS_FAKE_STATE"]) / "lease-checks").read_text().splitlines()
    assert checks
    assert set(checks) == {"verify selected-stores"}
    assert all("--bucket selected-stores " in line for line in _remote_writes(log))


@pytest.mark.parametrize(
    "missing", [None, "portfolio-dashboard", "outcomes", "latest-proposal", "proposal-history"]
)
@pytest.mark.parametrize("in_actions", [False, True])
def test_portfolio_publication_never_overwrites_the_deployed_legacy_dashboard(
    tmp_path, missing, in_actions
):
    root = _fake_repo(tmp_path)
    bin_dir, log = _fake_aws(tmp_path)
    environment = _environment(bin_dir, log)
    if in_actions:
        environment["GITHUB_ACTIONS"] = "true"
    export = tmp_path / "export"
    views = export / "views"
    views.mkdir(parents=True)
    # Even a stale artifact in a reused export directory must not reach the old key.
    for name in (
        "dashboard",
        "tasks",
        "portfolio-dashboard",
        "outcomes",
        "latest-proposal",
        "proposal-history",
    ):
        if name != missing:
            (views / f"{name}.json").write_text('{"synthetic": true}\n')
    completed = subprocess.run(
        [root / "jobs/scripts/r2_transfer.sh", "upload-portfolio-views", str(export)],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    commands = _transfer_commands(log)
    uploads = [
        command
        for command in commands
        if command.startswith("s3 cp ") and command.split()[3].startswith("s3://")
    ]
    if not in_actions:
        assert completed.returncode == 2
        assert uploads == []
        assert "refusing serving upload outside GitHub Actions" in completed.stderr
        return
    if missing is not None:
        assert completed.returncode == 2
        assert uploads == []
        assert f"missing portfolio view: {missing}" in completed.stderr
        return
    assert completed.returncode == 0, completed.stderr
    assert [command.split()[3] for command in uploads] == [
        f"s3://baibai-serving/views/{name}.json"
        for name in ("portfolio-dashboard", "outcomes", "latest-proposal", "proposal-history")
    ]
    assert not any("views/dashboard.json" in command for command in commands)
    assert not any("views/tasks.json" in command for command in commands)
    assert not any("delete-object" in command or "s3 rm" in command for command in commands)
    downloads = [command for command in commands if command.startswith("s3 cp s3://")]
    assert [command.split()[2] for command in downloads] == [
        f"s3://baibai-serving/views/{name}.json"
        for name in ("portfolio-dashboard", "outcomes", "latest-proposal", "proposal-history")
    ]
    assert "portfolio views verified from remote readback: 4" in completed.stdout
    assert '"synthetic": true' not in completed.stdout + completed.stderr
    assert not list(root.glob(".r2-transfer.*"))


@pytest.mark.parametrize("failure", ["FAIL", "CORRUPT", "RELEASE"])
@pytest.mark.parametrize(
    "name", ["portfolio-dashboard", "outcomes", "latest-proposal", "proposal-history"]
)
def test_portfolio_publication_refuses_failed_or_different_readback(tmp_path, failure, name):
    root = _fake_repo(tmp_path)
    bin_dir, log = _fake_aws(tmp_path)
    environment = _environment(bin_dir, log)
    environment["GITHUB_ACTIONS"] = "true"
    if failure == "RELEASE":
        environment["LEASE_FAKE_RELEASE_ON"] = f"s3 cp s3://baibai-serving/views/{name}.json "
    else:
        environment[f"AWS_FAKE_{failure}_DOWNLOAD_KEY"] = f"views/{name}.json"
    export = tmp_path / "export"
    views = export / "views"
    views.mkdir(parents=True)
    for view in ("portfolio-dashboard", "outcomes", "latest-proposal", "proposal-history"):
        (views / f"{view}.json").write_text('{"private": "synthetic-private-value"}\n')
    completed = subprocess.run(
        [root / "jobs/scripts/r2_transfer.sh", "upload-portfolio-views", str(export)],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode != 0
    assert "portfolio views verified" not in completed.stdout
    assert "synthetic-private-value" not in completed.stdout + completed.stderr
    assert "corrupted-private-payload" not in completed.stdout + completed.stderr
    assert not list(root.glob(".r2-transfer.*"))
    assert not any("delete-object" in command for command in _transfer_commands(log))


@pytest.mark.parametrize("kind", ["file", "symlink", "directory"])
@pytest.mark.parametrize("timing", ["before", "during_download"])
def test_application_pull_preserves_a_local_target_even_after_preflight(tmp_path, kind, timing):
    bin_dir, log = _fake_aws(tmp_path)
    root = _fake_repo(tmp_path)
    target = root / "stores/application/baibai.sqlite"
    target.parent.mkdir(parents=True)
    absent = tmp_path / "absent"
    env = _environment(bin_dir, log)
    env["LOCAL_APP"] = str(target)
    env["LOCAL_APP_KIND"] = kind
    env["LOCAL_APP_LINK"] = str(absent)
    generation = Path(env["R2_GENERATION_DIR"]) / "baibai.sqlite.etag"
    env["LOCAL_APP_GENERATION"] = str(generation)
    if timing == "before":
        if kind == "file":
            target.write_bytes(b"unpublished-local-judgment")
            generation.write_text('"local-origin"\n')
        elif kind == "symlink":
            target.symlink_to(absent)
        else:
            target.mkdir()
    else:
        aws = bin_dir / "aws"
        marker = 'if [[ "$1 $2" == "s3api get-object" ]]; then'
        injection = (
            marker
            + """
  case "$LOCAL_APP_KIND" in
    file)
      printf 'unpublished-local-judgment' > "$LOCAL_APP"
      printf '"local-origin"\n' > "$LOCAL_APP_GENERATION"
      ;;
    symlink) ln -s "$LOCAL_APP_LINK" "$LOCAL_APP" ;;
    directory) mkdir "$LOCAL_APP" ;;
  esac
"""
        )
        aws.write_text(aws.read_text().replace(marker, injection), encoding="utf-8")
    result = subprocess.run(
        [root / "jobs/scripts/r2_transfer.sh", "pull-app"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "refusing application store download overwrite" in result.stderr
    if kind == "file":
        assert target.read_bytes() == b"unpublished-local-judgment"
    elif kind == "symlink":
        assert target.is_symlink()
        assert target.readlink() == absent
        assert not absent.exists()
    else:
        assert target.is_dir()
        assert list(target.iterdir()) == []
    if kind == "file":
        assert generation.read_text() == '"local-origin"\n'
    else:
        assert not generation.exists()
    assert not list(root.glob(".r2-transfer.*"))
    commands = _transfer_commands(log)
    assert bool(commands) is (timing == "during_download")
    assert not any("put-object" in command or "delete-object" in command for command in commands)


def test_overlapping_application_pulls_keep_the_winning_copy_and_its_origin(tmp_path):
    bin_dir, log = _fake_aws(tmp_path)
    root = _fake_repo(tmp_path)
    env = _environment(bin_dir, log)
    generation = Path(env["R2_GENERATION_DIR"]) / "baibai.sqlite.etag"
    generation.write_text('"orphan"\n')
    command = root / "jobs/scripts/r2_transfer.sh"
    result_file = tmp_path / "other-pull-status"
    # Run a real second pull after the first has found no App, just before it
    # retires the orphan. Without local exclusion it deletes the winner's origin.
    remove = bin_dir / "rm"
    remove.write_text(
        """#!/usr/bin/env bash
if [[ -z "${SECOND_PULL:-}" && "$*" == "-f -- $R2_GENERATION_DIR/baibai.sqlite.etag" ]]; then
  SECOND_PULL=1 "$PULL_COMMAND" pull-app
  printf '%s\\n' "$?" > "$SECOND_STATUS"
fi
exec /bin/rm "$@"
"""
    )
    remove.chmod(0o755)
    outer = subprocess.run(
        [command, "pull-app"],
        cwd=root,
        env={**env, "PULL_COMMAND": str(command), "SECOND_STATUS": str(result_file)},
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )

    assert {outer.returncode, int(result_file.read_text())} == {0, 2}
    assert (root / "stores/application/baibai.sqlite").read_bytes() == b"x"
    assert generation.is_file(), "the losing pull removed the winning copy's origin"
    assert generation.read_text() == '"etag-stable"\n'
    assert [command.split()[:2] for command in _transfer_commands(log)] == [
        ["s3api", "get-object"],
        ["s3api", "head-object"],
    ]
    assert not list(root.glob(".r2-transfer.*"))


def test_application_pull_installs_complete_copy_and_generation(tmp_path):
    bin_dir, log = _fake_aws(tmp_path)
    root = _fake_repo(tmp_path)
    env = _environment(bin_dir, log)
    result = subprocess.run(
        [root / "jobs/scripts/r2_transfer.sh", "pull-app"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (root / "stores/application/baibai.sqlite").read_bytes() == b"x"
    assert (Path(env["R2_GENERATION_DIR"]) / "baibai.sqlite.etag").read_text() == '"etag-stable"\n'
    commands = _transfer_commands(log)
    assert [command.split()[:2] for command in commands] == [
        ["s3api", "get-object"],
        ["s3api", "head-object"],
    ]
    assert "--query [ETag, ContentLength] --output text" in commands[0]
    assert not list(root.glob(".r2-transfer.*"))


def test_application_pull_never_pairs_an_intermediate_body_with_the_restored_etag(tmp_path):
    bin_dir, log = _fake_aws(tmp_path)
    root = _fake_repo(tmp_path)
    env = _environment(bin_dir, log)
    # Both HEADs answer the original A. The body is B from between publication
    # and an explicit restore to A; the same GET response identifies it as B.
    (Path(env["AWS_FAKE_STATE"]) / "put-baibai.sqlite").write_bytes(b"current-body-B")
    env["AWS_FAKE_APP_GET_ETAG"] = '"etag-B"'
    generation = Path(env["R2_GENERATION_DIR"]) / "baibai.sqlite.etag"
    generation.write_text('"orphan-marker"\n')

    result = subprocess.run(
        [root / "jobs/scripts/r2_transfer.sh", "pull-app"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "changed on R2 during the pull" in result.stderr
    assert not (root / "stores/application/baibai.sqlite").exists()
    assert not generation.exists()
    assert not list(root.glob(".r2-transfer.*"))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("AWS_FAKE_APP_GET_ERROR", "1"),
        ("AWS_FAKE_APP_GET_METADATA", ""),
        ("AWS_FAKE_APP_GET_METADATA", "None\t1"),
        ("AWS_FAKE_APP_GET_METADATA", '"etag-stable"\t2'),
        ("AWS_FAKE_APP_GET_METADATA", '"etag-stable"\t1\tunexpected'),
        ("AWS_FAKE_APP_GET_METADATA", '"etag-stable"\t1\nsecond-response'),
    ],
)
def test_application_pull_refuses_bad_download_without_local_copy(tmp_path, field, value):
    bin_dir, log = _fake_aws(tmp_path)
    root = _fake_repo(tmp_path)
    env = _environment(bin_dir, log)
    env[field] = value

    result = subprocess.run(
        [root / "jobs/scripts/r2_transfer.sh", "pull-app"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "synthetic-private-value" not in result.stdout + result.stderr
    assert not (root / "stores/application/baibai.sqlite").exists()
    assert not (Path(env["R2_GENERATION_DIR"]) / "baibai.sqlite.etag").exists()
    assert not list(root.glob(".r2-transfer.*"))
    assert [command.split()[:2] for command in _transfer_commands(log)] == [
        ["s3api", "get-object"],
    ]


@pytest.mark.parametrize("cloud_changed", [False, True])
def test_indicator_push_publishes_reconciled_receipt_vintage(tmp_path, cloud_changed):
    import sys
    from contextlib import closing
    from datetime import UTC, date, timedelta

    from baibai_engine.data_collection.receipts.native_sources.confirmations import (
        confirm_source_response,
    )
    from baibai_engine.data_collection.receipts.native_sources.records import read_snapshots
    from baibai_engine.data_collection.receipts.native_sources.store import initialize_database

    root = _fake_repo(tmp_path)
    bin_dir, log = _fake_aws(tmp_path)
    environment = _environment(bin_dir, log)
    environment["SNAPSHOT_FAKE_COPY"] = "1"
    # Exercise the production CLI rather than the usual shell-only merge stub.
    uv = bin_dir / "uv"
    uv.write_text(
        uv.read_text().replace(
            "#!/usr/bin/env bash\n",
            "#!/usr/bin/env bash\n"
            'if [[ "$*" == *"baibai_jobs.integration.native_source_merge"* ]]; then\n'
            "  shift 2\n"
            f'  exec "{sys.executable}" "$@"\n'
            "fi\n",
            1,
        )
    )
    environment["PYTHONPATH"] = ":".join(
        str(REPO_ROOT / item) for item in ("engine/src", "jobs/src")
    )
    local = root / "stores/market_context/indicators.sqlite"
    local.parent.mkdir(parents=True)
    remote = Path(environment["AWS_FAKE_STATE"]) / "put-indicators.sqlite"
    day = date(2025, 1, 1)
    stamp = datetime(2026, 11, 1, tzinfo=UTC)

    def received(path, value, hour):
        with closing(initialize_database(path)) as connection, connection:
            confirm_source_response(
                connection,
                source_id="fred-test",
                fetched_at=stamp + timedelta(hours=hour),
                records_by_day={day: [{"value": value}]},
            )

    for path in (local, remote):
        received(path, "A", 1)
    if cloud_changed:
        received(remote, "B", 2)
    received(local, "A", 3)
    completed = subprocess.run(
        [root / "jobs/scripts/r2_transfer.sh", "push-indicators"],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    with closing(initialize_database(remote)) as connection:
        snapshot = next(read_snapshots(connection, day, day, source_id="fred-test"))
        assert snapshot.records == ({"value": "A"},)
        assert snapshot.fetched_at == stamp + timedelta(hours=3 if cloud_changed else 1)
        assert connection.execute("SELECT COUNT(*) FROM economic_source_records").fetchone()[0] == (
            3 if cloud_changed else 1
        )


def test_explicit_fred_recovery_is_bound_to_the_fresh_download_and_conditional_put(tmp_path):
    bin_dir, log = _fake_aws(tmp_path)
    completed = subprocess.run(
        [
            TRANSFER_SCRIPT,
            "push-indicators",
            "--recheck-source",
            "fred-vixcls",
            "--recheck-source",
            "fred-payems",
        ],
        cwd=REPO_ROOT,
        env=_environment(bin_dir, log),
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    commands = _transfer_commands(log)
    download = next(
        i
        for i, command in enumerate(commands)
        if command.startswith("s3 cp s3://baibai-stores/indicators.sqlite ")
    )
    recheck = next(i for i, command in enumerate(commands) if command.startswith("merge "))
    upload = next(
        i
        for i, command in enumerate(commands)
        if command.startswith("s3api put-object ") and "--key indicators.sqlite" in command
    )
    assert download < recheck < upload
    assert "--canonical" in commands[recheck]
    assert "--recheck-source fred-vixcls --recheck-source fred-payems" in commands[recheck]
    assert '--if-match "etag-stable"' in commands[upload]


def test_transfer_compacts_only_runs_and_never_uploads_failed_snapshots(tmp_path):
    bin_dir, log = _fake_aws(tmp_path)
    root = _fake_repo(tmp_path)
    env = _environment(bin_dir, log)
    env["GITHUB_ACTIONS"] = "true"
    snapshots = tmp_path / "snapshots.log"
    env["SNAPSHOT_LOG"] = str(snapshots)
    completed = subprocess.run(
        [root / "jobs/scripts/r2_transfer.sh", "push-machine"],
        cwd=root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    calls = snapshots.read_text().splitlines()
    creates = [line for line in calls if " create " in line]
    assert len(creates) == 2
    assert all(("--compact" in line) == ("/runs.sqlite" in line) for line in creates)
    # 初回PUTで変わったETagを取り直し、世代不一致ではなくcompact失敗まで到達させる。
    pulled = subprocess.run(
        [root / "jobs/scripts/r2_transfer.sh", "pull-machine"],
        cwd=root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert pulled.returncode == 0, pulled.stderr
    state = Path(env["AWS_FAKE_STATE"])
    unchanged_keys = ("runs.sqlite", "machine-manifest.json")
    before = {
        name: (state / name).read_bytes()
        for key in unchanged_keys
        for name in (f"put-{key}", f"generation-{key}")
    }
    log.write_text("")
    snapshots.write_text("")
    env["SNAPSHOT_COMPACT_FAKE_EXIT"] = "17"
    failed = subprocess.run(
        [root / "jobs/scripts/r2_transfer.sh", "push-machine"],
        cwd=root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert failed.returncode == 17, failed.stderr
    creates = [line for line in snapshots.read_text().splitlines() if " create " in line]
    assert len(creates) == 2
    assert "/market.sqlite" in creates[0]
    assert "--compact" not in creates[0]
    assert "/runs.sqlite" in creates[1]
    assert "--compact" in creates[1]
    # machineは逐次公開なので先行marketのbackup/PUTは済み、失敗runsとreceiptは進まない。
    writes = [
        line
        for line in _transfer_commands(log)
        if line.startswith(("s3api put-object ", "s3api copy-object "))
    ]
    assert len(writes) == 2
    assert writes[0].startswith("s3api copy-object ")
    assert "--key market.sqlite.bak " in writes[0]
    assert writes[1].startswith("s3api put-object ")
    assert "--key market.sqlite " in writes[1]
    assert '--if-match "etag-put-1"' in writes[1]
    assert (state / "generation-market.sqlite").read_bytes() == b"xx"
    assert {name: (state / name).read_bytes() for name in before} == before
