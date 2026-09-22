"""Connection smoke for unattended TradingView acquisition."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import date, datetime
from functools import partial
from pathlib import Path

from pydantic import ValidationError

from baibai_engine.foundation.repository_layout import MARKET_DB_PATH

from .auth import AuthenticationError, CredentialStorage, OAuthState, save_github_secret
from .client import connect, fetch_batch
from .collector import JST, collect
from .observations import COLUMNS


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


async def snapshot(
    state: OAuthState, repository: str, writer_token: str, path: Path, day: date, interval: float
) -> dict[str, object]:
    storage = CredentialStorage(
        state, partial(save_github_secret, repository=repository, writer_token=writer_token)
    )
    async with connect(storage) as session:

        async def fetch(symbols: list[str]) -> dict[str, object]:
            return await fetch_batch(session, symbols, COLUMNS)

        return await collect(path, day, fetch, interval=interval)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-engine tradingview")
    sub = parser.add_subparsers(dest="command", required=True)
    login = sub.add_parser("authorize", help="interactive local OAuth bootstrap")
    login.add_argument("--state-file", type=Path, required=True)
    sub.add_parser("smoke", help="refresh credentials and verify two symbols without storing facts")
    refresh = sub.add_parser("refresh", help="append today's full-universe post-close snapshot")
    refresh.add_argument("--sqlite", type=Path, default=MARKET_DB_PATH)
    refresh.add_argument("--asof", type=date.fromisoformat, default=None)
    refresh.add_argument("--interval", type=float, default=15.0)
    return parser


def main(argv: list[str] | None = None, /) -> int:
    args = build_parser().parse_args(argv)
    # SDK validation errors can contain token response values; report only safe status.
    logging.getLogger("mcp.client.auth").setLevel(logging.CRITICAL)
    try:
        if args.command == "authorize":
            from .authorize import authorize

            asyncio.run(authorize(args.state_file))
            return 0
        try:
            state = OAuthState.model_validate_json(os.environ.get("TRADINGVIEW_OAUTH_STATE", ""))
        except ValidationError:
            raise AuthenticationError("TradingView OAuth state is missing or invalid") from None
        repository = os.environ.get("GITHUB_REPOSITORY", "")
        writer_token = os.environ.get("TRADINGVIEW_SECRET_WRITER_TOKEN", "")
        if not repository or not writer_token:
            raise AuthenticationError("GitHub Secret writer credentials are missing")
        if args.command == "smoke":
            result = asyncio.run(smoke(state, repository, writer_token))
        else:
            day = args.asof or datetime.now(JST).date()
            result = asyncio.run(
                snapshot(state, repository, writer_token, args.sqlite, day, args.interval)
            )
    except Exception:
        # OAuth / HTTP exceptions may embed credential-bearing responses. Never
        # print their body, exception chain, environment, or serialized state.
        print(
            "TradingView acquisition failed; no snapshot committed; "
            "check provider and authentication",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
