"""One opt-in push retains actual request bodies; fake AWS never contacts R2."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest
from tests.helpers.r2_transfer import _environment, _fake_aws, _fake_repo

KEYS = ("market.sqlite", "runs.sqlite", "machine-manifest.json")


def _setup(tmp_path):
    root = _fake_repo(tmp_path)
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    evidence = tmp_path / "private-evidence"
    env["FAKE_EVIDENCE"] = str(evidence)
    env["AWS_FAKE_EXISTING_KEY"] = "market.sqlite:runs.sqlite:lake/pointers/market/current.json"
    env["AWS_MAX_ATTEMPTS"] = "9"
    env["AWS_RETRY_MODE"] = "adaptive"
    handle = Path(env["PUBLICATION_LEASE_HANDLE"])
    handle.write_text(
        json.dumps(
            {
                "owner": "operator:synthetic",
                "purpose": "daily",
                "etag": "lease-secret-not-exported",
                "acquired_at_utc": "2026-11-17T00:00:00+00:00",
                "expires_at_utc": "2026-11-17T03:40:00+00:00",
                "protocol_version": 2,
            }
        )
    )
    uv = bin_dir / "uv"
    uv_text = uv.read_text().replace(
        '"$(cat "$handle")" != synthetic-held',
        '"$(cat "$handle")" == synthetic-released',
    )
    uv_text = uv_text.replace(
        '  printf \'dehydrate %s\\n\' "$*" >> "$AWS_LOG"\n  exit 0',
        '  printf \'dehydrate %s\\n\' "$*" >> "$AWS_LOG"\n'
        "  while [[ $# -gt 0 ]]; do\n"
        '    if [[ $1 == --store ]]; then printf dehydrated >> "$2"; fi\n'
        "    shift\n"
        "  done\n"
        "  printf private-dehydrate-report\n  exit 0",
    )
    uv_text = uv_text.replace("  snapshot)\n", '  snapshot)\n    original_args="$*"\n').replace(
        "        printf 'x' > \"$output\"",
        "        printf 'x' > \"$output\"\n"
        '        if [[ $original_args == *--compact* ]]; then printf compact >> "$output"; fi',
    )
    uv.write_text(uv_text)
    for argv in (
        ["init", "-q"],
        ["config", "user.email", "test@example.invalid"],
        ["config", "user.name", "Fixture"],
        ["add", "jobs"],
        ["commit", "-qm", "synthetic transfer owner"],
    ):
        subprocess.run(["git", "-C", str(root), *argv], check=True, capture_output=True)
    (bin_dir / "aws").rename(bin_dir / "aws-original")
    (bin_dir / "aws").write_text(r"""#!/usr/bin/env python3
import hashlib, json, os, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
key = args[args.index('--key') + 1] if '--key' in args else ''
evidence = Path(os.environ['FAKE_EVIDENCE'])
state = Path(os.environ['AWS_FAKE_STATE'])
mode = os.environ.get('EVIDENCE_FAULT', '')
with (state / 'requests.jsonl').open('a') as stream:
    record = {'args': args, 'attempts': os.getenv('AWS_MAX_ATTEMPTS'),
              'retry_mode': os.getenv('AWS_RETRY_MODE')}
    if '--body' in args:
        body = Path(args[args.index('--body') + 1])
        record.update(body=str(body), sha256=hashlib.sha256(body.read_bytes()).hexdigest())
    stream.write(json.dumps(record) + '\n')
if evidence.exists() and mode.startswith('collision:'):
    path = evidence / mode.split(':', 1)[1]
    if not path.exists():
        path.write_text('prior retained evidence')
if args[:2] == ['s3api', 'copy-object'] and mode == 'body-before-put':
    (evidence / 'market.sqlite').write_bytes(b'changed after snapshot')
if args[:2] == ['s3api', 'put-object']:
    mode = mode if key == os.getenv('EVIDENCE_FAULT_KEY', 'market.sqlite') else ''
    if mode in {'403', '429', 'conflict', 'interrupt'}:
        print('private provider error at https://private.invalid', file=sys.stderr)
        sys.exit(130 if mode == 'interrupt' else 254)
    result = subprocess.run([str(Path(__file__).with_name('aws-original')), *args],
                            capture_output=True)
    if mode in {'after', 'empty', 'None', 'quoted-none', 'malformed', 'unbalanced'}:
        if mode == 'None': print('None')
        if mode == 'quoted-none': print('"None"')
        if mode == 'malformed': print('bad version with whitespace')
        if mode == 'unbalanced': print('"one-quote')
        sys.exit(253 if mode == 'after' else 0)
    if mode == 'ack-collision':
        (evidence / (key + '.put_ack.json')).write_text('prior retained evidence')
    if mode == 'body-after-put':
        (evidence / key).write_bytes(b'changed after request')
    if mode == 'completion-collision':
        (evidence / 'attempt.completed.json').write_text('prior retained evidence')
    if result.returncode == 0:
        print('"ack-' + key + '"')
    else:
        sys.stdout.buffer.write(result.stdout)
        sys.stderr.buffer.write(result.stderr)
    sys.exit(result.returncode)
result = subprocess.run([str(Path(__file__).with_name('aws-original')), *args])
sys.exit(result.returncode)
""")
    (bin_dir / "aws").chmod(0o755)
    return root, env, evidence, Path(env["AWS_FAKE_STATE"]), log


def _run(root, env, evidence, *extra):
    return subprocess.run(
        [root / "jobs/scripts/r2_transfer.sh", "push-machine", "--evidence-dir", evidence, *extra],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _read(path):
    return json.loads(path.read_bytes())


def _puts(state):
    if not (state / "requests.jsonl").exists():
        return []
    return [
        row
        for line in (state / "requests.jsonl").read_text().splitlines()
        if (row := json.loads(line))["args"][:2] == ["s3api", "put-object"]
    ]


def _key(row):
    return row["args"][row["args"].index("--key") + 1]


def test_retains_transformed_request_bodies_and_actual_ack_after_cleanup(tmp_path):
    root, env, evidence, state, log = _setup(tmp_path)
    result = _run(root, env, evidence)
    assert result.returncode == 0, result.stderr
    assert (evidence / "market.sqlite").read_bytes() == b"xdehydrated"
    assert (evidence / "runs.sqlite").read_bytes() == b"xcompact"
    assert [_key(row) for row in _puts(state)] == list(KEYS)
    assert not list(root.glob(".r2-transfer.*"))
    for key, request in zip(KEYS, _puts(state), strict=True):
        body = evidence / key
        assert Path(request["body"]) == body
        assert body.read_bytes() == (state / ("put-" + key)).read_bytes()
        ready = _read(evidence / (key + ".snapshot_ready.json"))
        start = _read(evidence / (key + ".put_started.json"))
        ack = _read(evidence / (key + ".put_ack.json"))
        assert ready["sha256"] == start["sha256"] == ack["sha256"] == request["sha256"]
        assert ready["size"] == body.stat().st_size
        assert request["attempts"] == "1"
        assert request["retry_mode"] == "standard"
        assert ack["put_response_etag"] == "ack-" + key
        assert ack["if_match"] == (None if key == KEYS[2] else '"etag-stable"')
    manifest = _read(evidence / "machine-manifest.json")
    assert manifest["keys"] == dict.fromkeys(KEYS[:2], "etag-put-1")
    assert "--if-match" not in _puts(state)[-1]["args"]
    assert _read(evidence / "attempt.completed.json")["readback_accepted"] is False
    assert not (evidence / "attempt.stopped.json").exists()
    assert evidence.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in evidence.iterdir())
    assert (
        _read(evidence / "attempt.started.json")["code_commit"]
        == subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    )
    all_json = "".join(path.read_text() for path in evidence.glob("*.json"))
    assert "lease-secret-not-exported" not in all_json
    assert "secret-for-test" not in all_json
    assert str(evidence) not in result.stdout
    assert "private-dehydrate-report" not in result.stdout
    assert not any("get-object" in line for line in log.read_text().splitlines())


@pytest.mark.parametrize("kind", ["directory", "file", "symlink", "dangling", "parent"])
def test_rejects_non_new_directory_before_any_remote_operation(tmp_path, kind):
    root, env, evidence, state, _ = _setup(tmp_path)
    if kind == "directory":
        evidence.mkdir()
    if kind == "file":
        evidence.write_bytes(b"old")
    if kind in {"symlink", "dangling"}:
        evidence.symlink_to(root if kind == "symlink" else tmp_path / "absent")
    if kind == "parent":
        evidence = tmp_path / "missing" / "child"
    result = _run(root, env, evidence)
    assert result.returncode != 0
    assert not (state / "requests.jsonl").exists()
    if kind == "file":
        assert evidence.read_bytes() == b"old"


@pytest.mark.parametrize("extra", [["unexpected"], ["--evidence-dir", "duplicate"]])
def test_rejects_extra_arguments_before_credentials_or_remote(tmp_path, extra):
    root, env, evidence, state, _ = _setup(tmp_path)
    for name in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        env.pop(name)
    result = _run(root, env, evidence, *extra)
    assert result.returncode == 2
    assert not evidence.exists()
    assert not (state / "requests.jsonl").exists()


@pytest.mark.parametrize("key", KEYS)
@pytest.mark.parametrize(
    "fault",
    [
        "403",
        "429",
        "conflict",
        "after",
        "empty",
        "None",
        "quoted-none",
        "malformed",
        "unbalanced",
        "ack-collision",
        "interrupt",
    ],
)
def test_unknown_put_stops_without_later_keys_or_resend(tmp_path, key, fault):
    root, env, evidence, state, _ = _setup(tmp_path)
    env.update(EVIDENCE_FAULT=fault, EVIDENCE_FAULT_KEY=key)
    result = _run(root, env, evidence)
    assert result.returncode != 0
    assert [_key(row) for row in _puts(state)] == list(KEYS[: KEYS.index(key) + 1])
    assert _read(evidence / "attempt.stopped.json")["outcome_unknown_key"] == key
    assert not (evidence / "attempt.completed.json").exists()
    assert (evidence / key).is_file()
    assert (evidence / (key + ".put_started.json")).is_file()
    assert "private.invalid" not in result.stderr
    if fault != "ack-collision":
        assert not (evidence / (key + ".put_ack.json")).exists()
    assert not list(root.glob(".r2-transfer.*"))


@pytest.mark.parametrize(
    "target",
    [
        "market.sqlite",
        "market.sqlite.snapshot_ready.json",
        "market.sqlite.backup_started.json",
        "market.sqlite.backup_checked.json",
        "market.sqlite.put_started.json",
    ],
)
def test_retention_or_receipt_collision_stops_before_put(tmp_path, target):
    root, env, evidence, state, _ = _setup(tmp_path)
    env["EVIDENCE_FAULT"] = "collision:" + target
    result = _run(root, env, evidence)
    assert result.returncode != 0
    assert not _puts(state)
    assert (evidence / target).read_text() == "prior retained evidence"
    assert not (evidence / "attempt.completed.json").exists()
    requests = [
        json.loads(line)["args"] for line in (state / "requests.jsonl").read_text().splitlines()
    ]
    backups = [args for args in requests if args[:2] == ["s3api", "copy-object"]]
    assert len(backups) == int(
        target in {"market.sqlite.backup_checked.json", "market.sqlite.put_started.json"}
    )


@pytest.mark.parametrize("fault", ["body-before-put", "body-after-put"])
def test_changed_retained_body_cannot_be_certified(tmp_path, fault):
    root, env, evidence, state, _ = _setup(tmp_path)
    env["EVIDENCE_FAULT"] = fault
    result = _run(root, env, evidence)
    assert result.returncode != 0
    assert [_key(row) for row in _puts(state)] == ([] if fault == "body-before-put" else [KEYS[0]])
    assert not (evidence / "market.sqlite.put_ack.json").exists()
    assert not (evidence / "attempt.completed.json").exists()


def test_completion_receipt_failure_is_not_a_completed_attempt(tmp_path):
    root, env, evidence, state, _ = _setup(tmp_path)
    env.update(EVIDENCE_FAULT="completion-collision", EVIDENCE_FAULT_KEY=KEYS[2])
    result = _run(root, env, evidence)
    assert result.returncode != 0
    assert len(_puts(state)) == 3
    assert (evidence / "attempt.completed.json").read_text() == "prior retained evidence"
    assert (evidence / "attempt.stopped.json").is_file()
    assert all((evidence / (key + ".put_ack.json")).is_file() for key in KEYS)


def test_failed_compaction_retains_prior_ack_and_never_puts_runs(tmp_path):
    root, env, evidence, state, _ = _setup(tmp_path)
    env["SNAPSHOT_COMPACT_FAKE_EXIT"] = "17"
    result = _run(root, env, evidence)
    assert result.returncode == 17
    assert [_key(row) for row in _puts(state)] == [KEYS[0]]
    assert (evidence / "market.sqlite.put_ack.json").is_file()
    assert not (evidence / "runs.sqlite.snapshot_ready.json").exists()
    assert not (evidence / "attempt.completed.json").exists()


def test_lease_expiry_after_backup_stops_before_put(tmp_path):
    root, env, evidence, state, _ = _setup(tmp_path)
    env["LEASE_FAKE_RELEASE_ON"] = "--key market.sqlite.bak"
    result = _run(root, env, evidence)
    assert result.returncode != 0
    assert not _puts(state)
    assert (evidence / "market.sqlite").is_file()
    assert (evidence / "market.sqlite.backup_started.json").is_file()
    assert not (evidence / "market.sqlite.put_started.json").exists()


@pytest.mark.parametrize("contents", [None, "not-json", "{}"])
def test_missing_or_incomplete_handle_stops_before_remote(tmp_path, contents):
    root, env, evidence, state, _ = _setup(tmp_path)
    handle = Path(env["PUBLICATION_LEASE_HANDLE"])
    if contents is None:
        handle.unlink()
    else:
        handle.write_text(contents)
    result = _run(root, env, evidence)
    assert result.returncode != 0
    assert not evidence.exists()
    assert not (state / "requests.jsonl").exists()


@pytest.mark.parametrize("failure", ["EXDEV", "ENOSPC"])
def test_link_failure_copies_only_for_cross_device(tmp_path, failure):
    root, env, evidence, state, _ = _setup(tmp_path)
    wrapper = tmp_path / "bin/python3"
    wrapper.write_text(
        f"#!{sys.executable}\n"
        "import errno, os, sys\n"
        "if sys.argv[1] != '-':\n"
        "    os.execv(sys.executable, [sys.executable, *sys.argv[1:]])\n"
        "sys.argv = sys.argv[1:]\n"
        "if sys.argv[2] == 'snapshot_ready':\n"
        f"    def fail(*args, **kwargs): raise OSError(errno.{failure}, 'synthetic')\n"
        "    os.link = fail\n"
        "exec(compile(sys.stdin.read(), '<evidence>', 'exec'))\n"
    )
    wrapper.chmod(0o755)
    result = _run(root, env, evidence)
    if failure == "EXDEV":
        assert result.returncode == 0, result.stderr
        for key in KEYS:
            assert (evidence / key).read_bytes() == (state / ("put-" + key)).read_bytes()
    else:
        assert result.returncode != 0
        assert not _puts(state)
        assert not (evidence / "market.sqlite").exists()
        assert not (evidence / "attempt.completed.json").exists()


def test_opt_in_adds_no_remote_requests_and_completed_dir_is_not_reused(tmp_path):
    calls = []
    for enabled in (False, True):
        case = tmp_path / str(enabled)
        case.mkdir()
        root, env, evidence, state, _ = _setup(case)
        result = (
            _run(root, env, evidence)
            if enabled
            else subprocess.run(
                [root / "jobs/scripts/r2_transfer.sh", "push-machine"],
                cwd=root,
                env=env,
                capture_output=True,
                check=False,
            )
        )
        assert result.returncode == 0, result.stderr
        rows = [json.loads(line) for line in (state / "requests.jsonl").read_text().splitlines()]
        calls.append([(row["args"][:2], _key(row)) for row in rows])
        if enabled:
            before = {path.name: path.read_bytes() for path in evidence.iterdir()}
            requests = (state / "requests.jsonl").read_bytes()
            assert _run(root, env, evidence).returncode != 0
            assert (state / "requests.jsonl").read_bytes() == requests
            assert {path.name: path.read_bytes() for path in evidence.iterdir()} == before
    assert calls[0] == calls[1]


def test_real_sqlite_snapshot_compaction_keeps_exact_request_after_cleanup(tmp_path):
    root, env, evidence, state, _ = _setup(tmp_path)
    # No lake pointer: only this test's SQLite backup/compact uses the real owner.
    # Dehydration and all remote behavior remain the separate synthetic cases above.
    env["AWS_FAKE_EXISTING_KEY"] = "market.sqlite:runs.sqlite"
    uv = tmp_path / "bin/uv"
    uv.write_text(
        uv.read_text().replace(
            "  snapshot)\n",
            f'  snapshot)\n    shift 4\n    exec "{sys.executable}" '
            '-m baibai_engine.storage.sqlite.snapshot "$@"\n',
        )
    )
    sources = [root / "stores/market/market.sqlite", root / "stores/screening/runs.sqlite"]
    for source in sources:
        source.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(source)) as db, db:
            db.execute("create table retained(value blob)")
            db.executemany("insert into retained values (?)", [(b"x" * 8192,)] * 40)
            db.execute("delete from retained where rowid > 2")
    before = [source.read_bytes() for source in sources]
    result = _run(root, env, evidence)
    assert result.returncode == 0, result.stderr
    assert [source.read_bytes() for source in sources] == before
    assert (evidence / "runs.sqlite").stat().st_size < sources[1].stat().st_size
    for key in KEYS[:2]:
        body = evidence / key
        assert body.read_bytes() == (state / ("put-" + key)).read_bytes()
        with closing(sqlite3.connect(f"{body.as_uri()}?mode=ro&immutable=1", uri=True)) as db:
            assert db.execute("pragma quick_check").fetchone() == ("ok",)
            assert db.execute("select count(*) from retained").fetchone() == (2,)
    assert not list(root.glob(".r2-transfer.*"))
