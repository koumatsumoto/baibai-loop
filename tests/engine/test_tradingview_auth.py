"""Credential rotation must survive a failed market-data request."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from baibai_engine.market.tradingview.auth import (
    AuthenticationError,
    CredentialStorage,
    OAuthState,
    save_github_secret,
    write_local_state,
)


def state() -> OAuthState:
    return OAuthState(
        client=OAuthClientInformationFull(
            client_id="test-client", redirect_uris=["http://127.0.0.1:8765/callback"]
        ),
        tokens=OAuthToken(
            access_token="old-access", refresh_token="old-refresh", token_type="Bearer"
        ),
        expires_at=1,
    )


def test_rotation_persists_before_returning(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    storage = CredentialStorage(state(), lambda value: write_local_state(path, value))
    updated = OAuthToken(
        access_token="new-access", refresh_token="new-refresh", token_type="Bearer", expires_in=900
    )
    assert storage.rotation_count == 0
    asyncio.run(storage.set_tokens(updated))
    assert storage.rotation_count == 1
    saved = OAuthState.model_validate_json(path.read_text())
    assert saved.tokens.refresh_token == "new-refresh"
    assert saved.expires_at > 1
    assert path.stat().st_mode & 0o777 == 0o600
    assert not path.with_name("credentials.json.tmp").exists()


def test_failed_durable_write_aborts_rotation() -> None:
    def fail(value: OAuthState) -> None:
        raise AuthenticationError("write failed")

    storage = CredentialStorage(state(), fail)
    with pytest.raises(AuthenticationError, match="write failed"):
        asyncio.run(storage.set_tokens(OAuthToken(access_token="new", token_type="Bearer")))
    assert storage.state.tokens.access_token == "old-access"
    assert storage.rotation_count == 0


def test_secret_writer_sends_state_only_on_stdin() -> None:
    with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as run:
        save_github_secret(state(), repository="owner/repo", writer_token="writer-secret")
    args, kwargs = run.call_args
    assert "old-access" not in repr(args)
    assert "writer-secret" not in repr(args)
    assert json.loads(kwargs["input"])["tokens"]["refresh_token"] == "old-refresh"
    assert kwargs["env"]["GH_TOKEN"] == "writer-secret"


def test_secret_writer_does_not_disclose_failure_response() -> None:
    with (
        patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess([], 1, stderr="private-token"),
        ),
        pytest.raises(AuthenticationError) as error,
    ):
        save_github_secret(state(), repository="owner/repo", writer_token="writer-secret")
    assert "private-token" not in str(error.value)


@pytest.mark.parametrize(
    "failure", [OSError("private-token"), subprocess.TimeoutExpired("private-token", 60)]
)
def test_secret_writer_process_failure_is_classified_without_payload(failure):
    from baibai_engine.market.tradingview.auth import SecretPersistenceError

    with (
        patch("subprocess.run", side_effect=failure),
        pytest.raises(SecretPersistenceError, match="could not complete") as error,
    ):
        save_github_secret(state(), repository="owner/repo", writer_token="writer-secret")
    assert "private-token" not in str(error.value)
