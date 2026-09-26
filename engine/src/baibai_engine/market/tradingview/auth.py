"""Official SDK authentication with durable refresh-token rotation.

The whole OAuth state is one GitHub Environment Secret. Persisting a rotation is
part of authentication, before any market observation can be accepted.
"""

from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404 - fixed gh command, credential JSON goes only to stdin
import time
from collections.abc import Callable
from pathlib import Path

from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import BaseModel, ConfigDict

from baibai_engine.foundation.filesystem import write_text_atomic

SECRET_NAME = "TRADINGVIEW_OAUTH_STATE"  # nosec B105 - GitHub Secret identifier
RUNTIME_ENVIRONMENT = "tradingview-runtime"


class AuthenticationError(RuntimeError):
    """Authentication or durable rotation failed; messages contain no credentials."""


class SecretPersistenceError(AuthenticationError):
    """Rotated credentials could not be durably saved."""


class OAuthState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client: OAuthClientInformationFull
    tokens: OAuthToken
    expires_at: float


def write_local_state(path: Path, state: OAuthState) -> None:
    """Replace private credential state without putting it in repository data."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_text_atomic(path, state.model_dump_json())


def save_github_secret(state: OAuthState, *, repository: str, writer_token: str) -> None:
    """Use gh's encrypted Secret API; neither credentials nor stderr enter logs."""
    if not repository or not writer_token:
        raise SecretPersistenceError("GitHub Secret writer credentials are missing")
    executable = shutil.which("gh")
    if executable is None:
        raise SecretPersistenceError("GitHub CLI is unavailable")
    # Fixed command, no shell; credentials travel through stdin and env.
    try:
        result = subprocess.run(  # nosec B603
            [
                executable,
                "secret",
                "set",
                SECRET_NAME,
                "--repo",
                repository,
                "--env",
                RUNTIME_ENVIRONMENT,
            ],
            input=state.model_dump_json(),
            text=True,
            capture_output=True,
            env={**os.environ, "GH_TOKEN": writer_token},
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise SecretPersistenceError("GitHub Secret write could not complete") from None
    if result.returncode:
        raise SecretPersistenceError("GitHub Secret rotation failed; reauthorize before retrying")


class CredentialStorage:
    """SDK TokenStorage; a failed durable write aborts the current fetch."""

    def __init__(self, state: OAuthState, persist: Callable[[OAuthState], None]) -> None:
        self.state = state
        self.persist = persist
        self.rotation_count = 0

    async def get_tokens(self) -> OAuthToken:
        return self.state.tokens

    async def get_client_info(self) -> OAuthClientInformationFull:
        return self.state.client

    async def set_tokens(self, tokens: OAuthToken) -> None:
        updated = self.state.model_copy(
            update={"tokens": tokens, "expires_at": time.time() + (tokens.expires_in or 0)}
        )
        self.persist(updated)
        self.state = updated
        self.rotation_count += 1

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        raise AuthenticationError("Unattended client registration is disabled; reauthorize locally")
