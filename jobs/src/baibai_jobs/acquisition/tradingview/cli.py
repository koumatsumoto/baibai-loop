"""Explicit maintenance of retained TradingView receipts; no provider connection."""

from __future__ import annotations

import argparse
import json
import os
import sys
from functools import partial
from pathlib import Path

from pydantic import ValidationError

from baibai_engine.data_collection.providers.tradingview.auth import (
    AuthenticationError,
    CredentialStorage,
    OAuthState,
    SecretPersistenceError,
    save_github_secret,
    write_local_state,
)
from baibai_jobs.maintenance.tradingview_capture import retain_capture
from baibai_jobs.observability.tradingview import failure_line


def credential_storage(state_file: Path | None) -> CredentialStorage:
    """Choose one explicit authority; a local failure must never use cloud state."""
    if state_file is not None:
        try:
            state = OAuthState.model_validate_json(state_file.read_bytes())
        except (OSError, ValidationError):
            raise AuthenticationError(
                "Local TradingView OAuth state is missing or invalid"
            ) from None

        def persist(updated: OAuthState) -> None:
            try:
                write_local_state(state_file, updated)
            except OSError:
                raise SecretPersistenceError("Local TradingView OAuth rotation failed") from None

        return CredentialStorage(state, persist)
    try:
        state = OAuthState.model_validate_json(os.environ.get("TRADINGVIEW_OAUTH_STATE", ""))
    except ValidationError:
        raise AuthenticationError("TradingView OAuth state is missing or invalid") from None
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    writer_token = os.environ.get("TRADINGVIEW_SECRET_WRITER_TOKEN", "")
    if not repository or not writer_token:
        raise SecretPersistenceError("GitHub Secret writer credentials are missing")
    return CredentialStorage(
        state, partial(save_github_secret, repository=repository, writer_token=writer_token)
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-jobs tradingview-source")
    sub = parser.add_subparsers(dest="command", required=True)
    receive = sub.add_parser("receive", help="retain one explicitly captured native MCP result")
    receive.add_argument("--sqlite", type=Path, required=True)
    receive.add_argument("--capture", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None, /) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = retain_capture(args.sqlite, args.capture)
    except Exception as exc:
        # OAuth / HTTP exceptions may embed credential-bearing responses. Never
        # print their body, exception chain, environment, or serialized state.
        print(
            failure_line(exc),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
