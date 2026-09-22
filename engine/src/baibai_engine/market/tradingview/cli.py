"""Connection smoke for unattended TradingView acquisition."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from functools import partial

from pydantic import ValidationError

from .auth import AuthenticationError, CredentialStorage, OAuthState, save_github_secret
from .client import connect, fetch_batch


async def smoke(state: OAuthState, repository: str, writer_token: str) -> dict[str, object]:
    storage = CredentialStorage(
        state, partial(save_github_secret, repository=repository, writer_token=writer_token)
    )
    # Exercise rotation rather than accepting a still-valid interactive token.
    storage.state = state.model_copy(update={"expires_at": 1.0})
    async with connect(storage) as session:
        result = await fetch_batch(
            session,
            ["TSE:7203", "TSE:8306"],
            ["close", "currency", "earnings_per_share_forecast_next_fy"],
        )
    print(
        json.dumps({"returned_rows": result.get("count"), "missing": result.get("missing_count")})
    )
    if result.get("count") != 2 or result.get("missing_count") != 0:
        raise RuntimeError("TradingView smoke did not return both requested symbols")
    return {"status": "ok", "rows": 2, "rotation_persisted": True}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-engine tradingview")
    parser.add_argument("command", choices=["smoke"])
    return parser


def main(argv: list[str] | None = None, /) -> int:
    build_parser().parse_args(argv)
    try:
        try:
            state = OAuthState.model_validate_json(os.environ.get("TRADINGVIEW_OAUTH_STATE", ""))
        except ValidationError:
            raise AuthenticationError("TradingView OAuth state is missing or invalid") from None
        repository = os.environ.get("GITHUB_REPOSITORY", "")
        writer_token = os.environ.get("TRADINGVIEW_SECRET_WRITER_TOKEN", "")
        if not repository or not writer_token:
            raise AuthenticationError("GitHub Secret writer credentials are missing")
        result = asyncio.run(smoke(state, repository, writer_token))
    except Exception:
        # OAuth / HTTP exceptions may embed credential-bearing responses. Never
        # print their body, exception chain, environment, or serialized state.
        print(
            "TradingView smoke failed; check authentication and Secret writer setup",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
