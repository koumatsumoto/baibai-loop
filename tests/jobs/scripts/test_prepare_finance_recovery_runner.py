import importlib.util
import shutil
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parents[3] / "jobs/scripts/prepare_finance_recovery_runner.py"
_SPEC = importlib.util.spec_from_file_location("finance_runner_preparation", _SCRIPT)
assert _SPEC is not None
assert _SPEC.loader is not None
helper = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(helper)


@pytest.fixture
def hosted(tmp_path):
    env = dict(helper._REQUIRED_ENV)
    env["GITHUB_WORKSPACE"] = "/home/runner/work/baibai-loop/baibai-loop"
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc/os-release").write_text('ID=ubuntu\nVERSION_ID="24.04"\n')
    (tmp_path / env["GITHUB_WORKSPACE"].lstrip("/")).mkdir(parents=True)
    for root in helper._SDK_ROOTS:
        path = tmp_path / root.lstrip("/")
        path.mkdir(parents=True)
        (path / "fixture").write_bytes(b"SDK fixture")
    return env, tmp_path


def test_only_allowlisted_fake_sdks_removed_and_capacity_never_approved(hosted):
    env, root = hosted
    protected = root / "opt/hostedtoolcache/python"
    protected.mkdir(parents=True)
    result = helper.prepare(env, root=root, remove=shutil.rmtree)
    assert result["removed_sdk_roots"] == list(helper._SDK_ROOTS)
    assert result["capacity_approved"] is False
    assert protected.exists()


@pytest.mark.parametrize("name", list(helper._REQUIRED_ENV))
def test_wrong_runner_marker_fails_before_removal(hosted, name):
    env, root = hosted
    env[name] = "unexpected"
    calls = []
    with pytest.raises(ValueError, match="trusted one-shot"):
        helper.prepare(env, root=root, remove=calls.append)
    assert not calls


@pytest.mark.parametrize("name", helper._CREDENTIALS)
def test_credentials_must_not_be_injected(hosted, name):
    env, root = hosted
    env[name] = "fake-test-value"
    calls = []
    with pytest.raises(ValueError, match="before credential"):
        helper.prepare(env, root=root, remove=calls.append)
    assert not calls


def test_all_targets_validated_before_any_removal(hosted):
    env, root = hosted
    target = root / helper._SDK_ROOTS[-1].lstrip("/")
    shutil.rmtree(target)
    target.symlink_to(root / "etc", target_is_directory=True)
    calls = []
    with pytest.raises(ValueError, match="symlink"):
        helper.prepare(env, root=root, remove=calls.append)
    assert not calls
    assert (root / "etc/os-release").exists()


def test_internal_symlink_target_is_not_followed(hosted):
    env, root = hosted
    target = root / helper._SDK_ROOTS[0].lstrip("/")
    (target / "outside").symlink_to(root / "etc", target_is_directory=True)
    helper.prepare(env, root=root, remove=shutil.rmtree)
    assert (root / "etc/os-release").exists()


def test_wrong_os_release_stops(hosted):
    env, root = hosted
    (root / "etc/os-release").write_text('ID=ubuntu\nVERSION_ID="22.04"\n')
    with pytest.raises(ValueError, match=r"Ubuntu 24\.04"):
        helper.prepare(env, root=root, remove=shutil.rmtree)


def test_protected_runtime_inside_sdk_stops(hosted):
    env, root = hosted
    env["RUNNER_TOOL_CACHE"] = helper._SDK_ROOTS[0] + "/python"
    with pytest.raises(ValueError, match="protected"):
        helper.prepare(env, root=root, remove=shutil.rmtree)


def test_failed_removal_is_not_capacity_success(hosted):
    env, root = hosted
    with pytest.raises(ValueError, match="incomplete"):
        helper.prepare(env, root=root, remove=lambda path: None)


def test_symlink_ancestor_fails_before_removal(hosted):
    env, root = hosted
    parent = root / "usr/share"
    renamed = root / "actual-share"
    parent.rename(renamed)
    parent.symlink_to(renamed, target_is_directory=True)
    calls = []
    with pytest.raises(ValueError, match="symlink"):
        helper.prepare(env, root=root, remove=calls.append)
    assert not calls


def test_main_rejects_any_path_or_option_argument(monkeypatch):
    monkeypatch.setattr(helper.sys, "argv", ["prepare", "/untrusted"])
    with pytest.raises(ValueError, match="no arguments"):
        helper.main()
