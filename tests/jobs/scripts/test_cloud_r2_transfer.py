from __future__ import annotations

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

_SERVING_VIEW_ORDER = (
    "portfolio-dashboard",
    "outcomes",
    "latest-proposal",
    "proposal-history",
    "latest-us-proposal",
)


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
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def test_sqlite_helpers_run_from_the_repository_when_called_elsewhere(tmp_path: Path) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    root = _fake_repo(tmp_path)
    environment = _environment(bin_dir, log)
    environment["UV_EXPECTED_CWD"] = str(root)

    completed = subprocess.run(
        [root / "jobs/scripts/r2_transfer.sh", "pull-indicators"],
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
        "pull-portfolio-market",
    }
    assert naming_app == {"pull-app"}


def _push_app(tmp_path: Path) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
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


_INDICATOR_GET = "s3api get-object --bucket baibai-stores --key indicators.sqlite "


def _indicator_fixture(path: Path, value: str) -> bytes:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE sample (value TEXT NOT NULL)")
        connection.execute("INSERT INTO sample VALUES (?)", (value,))
    return path.read_bytes()


@pytest.mark.parametrize("operation", ["pull-indicators", "push-indicators"])
@pytest.mark.parametrize("failure", ["aba", "wrong_etag", "wrong_length"])
def test_indicator_download_refuses_unproven_body(tmp_path, operation, failure):
    root = _fake_repo(tmp_path)
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    state = Path(env["AWS_FAKE_STATE"])
    canonical = _indicator_fixture(state / "put-indicators.sqlite", "a")
    local_path = root / "stores/market_context/indicators.sqlite"
    local_path.parent.mkdir(parents=True)
    local = _indicator_fixture(local_path, "c")
    if failure == "aba":
        transient = _indicator_fixture(state / "transient-indicators.sqlite", "b")
        assert transient != canonical
        assert len(transient) == len(canonical)
    else:
        etag = '"etag-other"' if failure == "wrong_etag" else '"etag-stable"'
        length = len(canonical) + (failure == "wrong_length")
        (state / "get-metadata-indicators.sqlite").write_text(f"{etag}\t{length}\n")

    completed = subprocess.run(
        [root / "jobs/scripts/r2_transfer.sh", operation],
        cwd=root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    commands = _transfer_commands(log)
    downloads = [command for command in commands if command.startswith(_INDICATOR_GET)]
    assert len(downloads) == 1
    assert '--if-match "etag-stable"' in downloads[0]
    assert not any(command.startswith("merge ") for command in commands)
    assert _remote_writes(log) == []
    assert "synthetic-private-value" not in completed.stderr
    assert local_path.read_bytes() == local
    assert not list(root.glob(".r2-transfer.*"))


def test_indicator_pull_installs_the_single_pinned_get(tmp_path):
    root = _fake_repo(tmp_path)
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    # Indicator pushes merge a fresh remote copy; only the App needs a local origin.
    unavailable_origin_dir = tmp_path / "not-a-directory"
    unavailable_origin_dir.write_text("untouched")
    env["R2_GENERATION_DIR"] = str(unavailable_origin_dir)
    canonical = _indicator_fixture(Path(env["AWS_FAKE_STATE"]) / "put-indicators.sqlite", "a")

    completed = subprocess.run(
        [root / "jobs/scripts/r2_transfer.sh", "pull-indicators"],
        cwd=root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    commands = _transfer_commands(log)
    downloads = [command for command in commands if command.startswith(_INDICATOR_GET)]
    assert len(downloads) == 1
    assert '--if-match "etag-stable"' in downloads[0]
    assert sum(command.startswith("s3api head-object ") for command in commands) == 2
    assert _remote_writes(log) == []
    assert (root / "stores/market_context/indicators.sqlite").read_bytes() == canonical
    assert unavailable_origin_dir.read_text() == "untouched"


def test_indicator_push_merges_the_cloud_store_before_uploading(tmp_path: Path) -> None:
    # Runs outside GitHub Actions on purpose: the deep history this publishes is fetched
    # locally, and the merge — not the caller's environment — is what guarantees the cloud
    # store is not rolled back.
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    old_backup = Path(env["AWS_FAKE_STATE"]) / "put-indicators.sqlite.bak"
    old_backup.write_bytes(b"old backup is not a publication input")

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
        index for index, command in enumerate(commands) if command.startswith(_INDICATOR_GET)
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
    assert old_backup.read_bytes() == b"old backup is not a publication input"


@pytest.mark.parametrize("failure", ["lease", "generation"])
def test_indicator_push_checks_lease_and_generation_after_merging(tmp_path, failure):
    root = _fake_repo(tmp_path)
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    if failure == "lease":
        env["LEASE_FAKE_RELEASE_ON"] = _INDICATOR_GET
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


@pytest.mark.parametrize(
    "args",
    [
        (),
        ("unknown-command",),
        ("pull-app", "extra"),
        ("pull-machine", "extra"),
        ("pull-portfolio-market", "extra"),
        ("pull-indicators", "extra"),
        ("push-app", "extra"),
        ("pull-market",),
        ("pull-runs",),
        ("hydrate-market",),
        ("hydrate-market-for-daily", "extra"),
        ("hydrate-portfolio",),
        ("seed-all",),
        ("publish-lake",),
        ("push-market",),
        ("push-runs",),
        ("push-machine",),
        ("push-machine", "--daily-notice", "notice.json"),
        ("push-machine", "--evidence-dir", "output"),
        ("restore-machine-receipt", "receipt.json", "a" * 64),
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


def _remote_writes(log):
    return [
        command
        for command in _transfer_commands(log)
        if command.startswith(("s3api put-object ", "s3api copy-object "))
        or (command.startswith("s3 cp ") and command.split()[3].startswith("s3://"))
    ]


@pytest.mark.parametrize(
    "operation",
    ["push-indicators", "upload-portfolio-views"],
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


@pytest.mark.parametrize("released_after", ["portfolio-dashboard", "proposal-history"])
def test_serving_transfer_stops_the_next_write_after_lease_release(tmp_path, released_after):
    root = _fake_repo(tmp_path)
    binary, log = _fake_aws(tmp_path)
    env = _environment(binary, log)
    env["GITHUB_ACTIONS"] = "true"
    env["LEASE_FAKE_RELEASE_ON"] = f"s3://baibai-serving/views/{released_after}.json"
    views = tmp_path / "serving/views"
    views.mkdir(parents=True)
    for name in _SERVING_VIEW_ORDER:
        (views / f"{name}.json").write_text('{"synthetic": true}')
    result = subprocess.run(
        [root / "jobs/scripts/r2_transfer.sh", "upload-portfolio-views", views.parent],
        env=env,
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "publication lease" in result.stderr
    writes = _remote_writes(log)
    assert [command.split()[3] for command in writes] == [
        f"s3://baibai-serving/views/{name}.json"
        for name in _SERVING_VIEW_ORDER[: _SERVING_VIEW_ORDER.index(released_after) + 1]
    ]
    assert "portfolio views verified" not in result.stdout
    assert not list(root.glob(".r2-transfer.*"))


def test_application_push_does_not_require_the_common_lease(tmp_path):
    root = _fake_repo(tmp_path)
    binary, log = _fake_aws(tmp_path)
    env = _environment(binary, log)
    env.pop("PUBLICATION_LEASE_HANDLE")
    (Path(env["R2_GENERATION_DIR"]) / "baibai.sqlite.etag").write_text('"etag-stable"\n')
    result = subprocess.run(
        [root / "jobs/scripts/r2_transfer.sh", "push-app"],
        env=env,
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert len(_remote_writes(log)) == 1
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
        [root / "jobs/scripts/r2_transfer.sh", "push-indicators"],
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
    ("invalid", "shape"),
    [
        (None, None),
        *((name, "missing") for name in _SERVING_VIEW_ORDER),
        ("latest-us-proposal", "empty"),
        ("latest-us-proposal", "directory"),
    ],
)
@pytest.mark.parametrize("in_actions", [False, True])
def test_portfolio_publication_never_overwrites_the_deployed_legacy_dashboard(
    tmp_path, invalid, shape, in_actions
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
    for name in ("dashboard", "tasks", *_SERVING_VIEW_ORDER):
        file = views / f"{name}.json"
        if name != invalid:
            file.write_text(f'{{"synthetic-private-value": "{name}"}}\n')
        elif shape == "empty":
            file.touch()
        elif shape == "directory":
            file.mkdir()
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
    if invalid is not None:
        assert completed.returncode == 2
        assert commands == []
        assert f"missing portfolio view: {invalid}" in completed.stderr
        return
    assert completed.returncode == 0, completed.stderr
    expected_urls = [f"s3://baibai-serving/views/{name}.json" for name in _SERVING_VIEW_ORDER]
    assert [command.split()[3] for command in uploads] == expected_urls
    assert all(
        "--content-type application/json --cache-control no-store " in cmd for cmd in uploads
    )
    assert not any("views/dashboard.json" in command for command in commands)
    assert not any("views/tasks.json" in command for command in commands)
    assert not any("delete-object" in command or "s3 rm" in command for command in commands)
    downloads = [command for command in commands if command.startswith("s3 cp s3://")]
    assert [command.split()[2] for command in downloads] == expected_urls
    assert completed.stdout == "portfolio views verified from remote readback: 5\n"
    assert "synthetic-private-value" not in completed.stdout + completed.stderr
    assert not list(root.glob(".r2-transfer.*"))


@pytest.mark.parametrize("failure", ["UPLOAD", "FAIL", "CORRUPT", "RELEASE"])
@pytest.mark.parametrize("name", _SERVING_VIEW_ORDER)
def test_portfolio_publication_refuses_failed_transfer_or_readback(tmp_path, failure, name):
    root = _fake_repo(tmp_path)
    bin_dir, log = _fake_aws(tmp_path)
    environment = _environment(bin_dir, log)
    environment["GITHUB_ACTIONS"] = "true"
    if failure == "RELEASE":
        environment["LEASE_FAKE_RELEASE_ON"] = f"s3 cp s3://baibai-serving/views/{name}.json "
    elif failure == "UPLOAD":
        environment["AWS_FAKE_FAIL_UPLOAD_KEY"] = f"views/{name}.json"
    else:
        environment[f"AWS_FAKE_{failure}_DOWNLOAD_KEY"] = f"views/{name}.json"
    export = tmp_path / "export"
    views = export / "views"
    views.mkdir(parents=True)
    for view in _SERVING_VIEW_ORDER:
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
    commands = _transfer_commands(log)
    assert not any("delete-object" in command for command in commands)
    if failure == "UPLOAD":
        assert f"portfolio view upload failed: {name}" in completed.stderr
        assert not any(command.startswith("s3 cp s3://") for command in commands)


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
    download = next(i for i, command in enumerate(commands) if command.startswith(_INDICATOR_GET))
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
