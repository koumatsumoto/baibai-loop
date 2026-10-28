"""Connection smoke for unattended TradingView acquisition."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import tempfile
from contextlib import suppress
from dataclasses import asdict
from datetime import date, datetime
from functools import partial
from pathlib import Path

from mcp.types import CallToolResult
from pydantic import ValidationError

from baibai_engine.data_collection.providers.tradingview.auth import (
    AuthenticationError,
    CredentialStorage,
    OAuthState,
    SecretPersistenceError,
    save_github_secret,
    write_local_state,
)
from baibai_engine.data_collection.providers.tradingview.client import (
    connect,
    fetch_batch,
)
from baibai_engine.machine_analysis.observations.external_assessments.tradingview import (
    observations as observations_owner,
)
from baibai_engine.machine_analysis.observations.external_assessments.tradingview.decode import (
    decode_tool_result,
)
from baibai_engine.storage.paths import MARKET_DB_PATH
from baibai_jobs.acquisition.tradingview.collector import (
    JST,
    TimeGuardError,
    collect,
    preflight_snapshot,
)
from baibai_jobs.maintenance.tradingview_rebuild import (
    RebuildError,
    rebuild_from_receipts,
)
from baibai_jobs.observability.tradingview import (
    CollectionProgress,
    failure_category,
    failure_line,
    safe_progress,
)


def _write_progress_file(path: Path, payload: dict[str, object]) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(safe_progress(payload), stream, allow_nan=False)
        temporary.replace(path)
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)


async def smoke(storage: CredentialStorage) -> dict[str, object]:
    # Exercise rotation rather than accepting a still-valid interactive token.
    storage.state = storage.state.model_copy(update={"expires_at": 1.0})
    async with connect(storage) as session:
        received = await fetch_batch(
            session,
            ["TSE:7203", "TSE:8306"],
            ["close", "currency", "earnings_per_share_forecast_next_fy"],
        )
    result = decode_tool_result(
        json.dumps(
            received.model_dump(mode="python", by_alias=True),
            ensure_ascii=False,
            allow_nan=False,
        )
    )
    print(
        json.dumps({"returned_rows": result.get("count"), "missing": result.get("missing_count")})
    )
    if result.get("count") != 2 or result.get("missing_count") != 0:
        raise observations_owner.FetchError(
            "TradingView smoke did not return both requested symbols"
        )
    return {"status": "ok", "rows": 2, "rotation_persisted": True}


async def snapshot(
    storage: CredentialStorage,
    path: Path,
    day: date,
    interval: float,
    progress_output: Path | None = None,
    *,
    master_asof: date | None = None,
) -> dict[str, object]:
    output_enabled = progress_output is not None

    def progress(value: CollectionProgress) -> None:
        nonlocal output_enabled
        if not output_enabled or progress_output is None:
            return
        try:
            _write_progress_file(
                progress_output, {**asdict(value), "oauth_rotations": storage.rotation_count}
            )
        except Exception as exc:
            output_enabled = False
            print(
                f"TradingView progress output disabled; type={type(exc).__name__}", file=sys.stderr
            )

    result: dict[str, object] | None = None
    try:
        async with connect(storage) as session:

            async def fetch(symbols: list[str], columns: list[str]) -> CallToolResult:
                return await fetch_batch(session, symbols, columns)

            result = await collect(
                path,
                day,
                fetch,
                interval=interval,
                progress=progress,
                master_asof=master_asof,
            )
    except Exception as exc:
        category = failure_category(exc)
        added = result.get("rows_added") if result is not None else None
        if (
            result is None
            or result.get("status") not in {"saved", "partial"}
            or not isinstance(added, int)
            or added <= 0
            or category in {"internal", "storage"}
        ):
            raise
        result = {**result, "stop_reason": result.get("stop_reason") or category}
    assert result is not None
    return {**result, "oauth_rotations": storage.rotation_count}


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
    login = sub.add_parser("authorize", help="interactive local OAuth bootstrap")
    login.add_argument("--state-file", type=Path, required=True)
    smoke_parser = sub.add_parser(
        "smoke", help="refresh credentials and verify two symbols without storing facts"
    )
    smoke_parser.add_argument(
        "--state-file", type=Path, help="independent local OAuth file; never writes GitHub Secrets"
    )
    refresh = sub.add_parser(
        "refresh",
        help="append today's permitted business/rest-day observation, resuming missing symbols",
    )
    refresh.add_argument(
        "--state-file", type=Path, help="independent local OAuth file; never writes GitHub Secrets"
    )
    refresh.add_argument("--sqlite", type=Path, default=MARKET_DB_PATH)
    refresh.add_argument("--asof", type=date.fromisoformat, default=None)
    refresh.add_argument(
        "--master-asof",
        type=date.fromisoformat,
        help="assert the calendar-required saved master date; never bypass observation hours",
    )
    refresh.add_argument("--progress-output", type=Path)
    refresh.add_argument(
        "--interval", type=float, default=15.0, help="request interval, 15..3600 seconds"
    )
    rebuild = sub.add_parser("rebuild", help="rebuild one date from explicit saved MCP receipts")
    rebuild.add_argument("--sqlite", type=Path, required=True)
    rebuild.add_argument("--asof", type=date.fromisoformat, required=True)
    rebuild.add_argument("--receipt-id", action="append", required=True)
    return parser


def main(argv: list[str] | None = None, /) -> int:
    args = build_parser().parse_args(argv)
    # SDK validation errors can contain token response values; report only safe status.
    logging.getLogger("mcp.client.auth").setLevel(logging.CRITICAL)
    try:
        if args.command == "rebuild":
            result = rebuild_from_receipts(args.sqlite, args.asof, args.receipt_id)
            print(json.dumps(result, sort_keys=True))
            return 0
        if args.command == "authorize":
            from baibai_engine.data_collection.providers.tradingview.authorize import authorize

            asyncio.run(authorize(args.state_file))
            return 0
        if args.command == "refresh":
            now = datetime.now(JST)
            day = args.asof or now.date()
            if args.master_asof is None and day < now.date():
                print(
                    json.dumps(
                        {"status": "skipped_historical_asof", "snapshot_date": day.isoformat()}
                    )
                )
                return 0
            if day != now.astimezone(JST).date():
                raise TimeGuardError("Snapshots require today's observation; backfill is forbidden")
            if not 15 <= args.interval <= 3600:
                raise TimeGuardError("Observation interval must be between 15 and 3600 seconds")
            if args.master_asof is not None:
                preflight = preflight_snapshot(
                    args.sqlite, day, master_asof=args.master_asof, now=now
                )
            else:
                preflight = preflight_snapshot(args.sqlite, day, now=now)
            if preflight["status"] != "needs_fetch":
                print(json.dumps({**preflight, "oauth_rotations": 0}))
                return 0
        storage = credential_storage(args.state_file)
        if args.command == "smoke":
            result = asyncio.run(smoke(storage))
        else:
            result = asyncio.run(
                snapshot(
                    storage,
                    args.sqlite,
                    day,
                    args.interval,
                    args.progress_output,
                    master_asof=args.master_asof,
                )
            )
    except RebuildError as exc:
        print(json.dumps({"status": "failed", "rejected_receipts": exc.rejected}), file=sys.stderr)
        return 1
    except Exception as exc:
        # OAuth / HTTP exceptions may embed credential-bearing responses. Never
        # print their body, exception chain, environment, or serialized state.
        print(
            failure_line(exc),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
