"""CLI credential selection and persistence boundaries; no real credentials."""

import asyncio

import pytest
from mcp.shared.auth import OAuthToken
from tests.helpers.tradingview_auth import state

from baibai_engine.data_collection.providers.tradingview.auth import (
    AuthenticationError,
    write_local_state,
)


def test_local_rotation_is_independent_and_reloaded(tmp_path, monkeypatch):
    import baibai_jobs.acquisition.tradingview.cli as cli

    path = tmp_path / "oauth.json"
    write_local_state(path, state())
    monkeypatch.setenv("TRADINGVIEW_OAUTH_STATE", "cloud-state-must-not-be-read")
    monkeypatch.setenv("TRADINGVIEW_SECRET_WRITER_TOKEN", "cloud-writer-must-not-be-used")
    monkeypatch.setattr(cli, "save_github_secret", lambda *a, **k: pytest.fail("cloud write"))
    storage = cli.credential_storage(path)
    asyncio.run(
        storage.set_tokens(
            OAuthToken(access_token="local-new", refresh_token="local-refresh", token_type="Bearer")
        )
    )
    next_process = cli.credential_storage(path)
    assert next_process.state.tokens.refresh_token == "local-refresh"
    assert path.stat().st_mode & 0o777 == 0o600
    assert storage.rotation_count == 1



@pytest.mark.parametrize("kind", ["missing", "invalid", "unreadable"])
def test_bad_local_state_never_falls_back_to_cloud(tmp_path, monkeypatch, kind):
    import baibai_jobs.acquisition.tradingview.cli as cli

    path = tmp_path / "oauth.json"
    monkeypatch.setenv("TRADINGVIEW_OAUTH_STATE", state().model_dump_json())
    monkeypatch.setenv("TRADINGVIEW_SECRET_WRITER_TOKEN", "writer")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    if kind == "invalid":
        path.write_text("private-token")
    if kind == "unreadable":
        path.mkdir()
    with pytest.raises(AuthenticationError) as error:
        cli.credential_storage(path)
    assert "private-token" not in str(error.value)



def test_local_save_failure_does_not_adopt_tokens_or_leak_payload(tmp_path, monkeypatch, capsys):
    import baibai_jobs.acquisition.tradingview.cli as cli
    from baibai_engine.data_collection.providers.tradingview.auth import SecretPersistenceError

    path = tmp_path / "oauth.json"
    write_local_state(path, state())
    storage = cli.credential_storage(path)

    def fail(*args):
        raise OSError("private-token")

    monkeypatch.setattr(cli, "write_local_state", fail)
    with pytest.raises(SecretPersistenceError) as error:
        asyncio.run(storage.set_tokens(OAuthToken(access_token="new-private", token_type="Bearer")))
    assert storage.state.tokens.access_token == "old-access"
    assert storage.rotation_count == 0
    assert "private" not in str(error.value)
    assert capsys.readouterr().out == ""



def test_implicit_cloud_storage_still_writes_environment_secret(monkeypatch):
    import baibai_jobs.acquisition.tradingview.cli as cli

    monkeypatch.setenv("TRADINGVIEW_OAUTH_STATE", state().model_dump_json())
    monkeypatch.setenv("TRADINGVIEW_SECRET_WRITER_TOKEN", "cloud-writer")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    calls = []
    monkeypatch.setattr(cli, "save_github_secret", lambda value, **kw: calls.append((value, kw)))
    storage = cli.credential_storage(None)
    asyncio.run(storage.set_tokens(OAuthToken(access_token="new", token_type="Bearer")))
    assert calls[0][1] == {"repository": "owner/repo", "writer_token": "cloud-writer"}
