"""Bind a successful daily model publication to its own GitHub Actions attempt."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from baibai_engine.storage.paths import MARKET_DB_PATH
from baibai_engine.storage.sqlite.source_cache.lake_origin import (
    LakeStoreOrigin,
    LakeStoreOriginError,
    read_lake_store_origin,
)


def daily_run(value: str) -> str:
    if re.fullmatch(r"[1-9][0-9]{0,19}-[1-9][0-9]{0,9}", value) is None:
        raise ValueError("invalid original daily run ID and attempt")
    return value


def current_daily_run() -> str:
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    ref = os.environ.get("GITHUB_REF", "")
    if (
        not repository
        or not ref
        or os.environ.get("GITHUB_WORKFLOW_REF")
        != f"{repository}/.github/workflows/cloud-daily-batch.yml@{ref}"
    ):
        raise ValueError("daily publication requires the current daily workflow")
    return daily_run(
        f"{os.environ.get('GITHUB_RUN_ID', '')}-{os.environ.get('GITHUB_RUN_ATTEMPT', '')}"
    )


def origin_fields(run: str, lake: LakeStoreOrigin) -> dict[str, str]:
    return {
        "run": daily_run(run),
        "release_id": lake.release_id,
        "release_manifest_sha256": lake.release_manifest_sha256,
    }


def origin_for_push(notice: Path, market: Path) -> dict[str, str]:
    current = current_daily_run()
    data = json.loads(notice.read_bytes())
    if (
        not isinstance(data, dict)
        or data.get("models_ready") is not True
        or data.get("daily_run") != current
    ):
        raise ValueError("model publication is not a success of the current daily attempt")
    lake = data.get("lake")
    if (
        not isinstance(lake, dict)
        or not isinstance(lake.get("release_id"), str)
        or not isinstance(lake.get("release_manifest_sha256"), str)
    ):
        raise ValueError("daily model publication did not identify its lake release")
    expected = LakeStoreOrigin(lake["release_id"], lake["release_manifest_sha256"])
    if read_lake_store_origin(market) != expected:
        raise ValueError("local market origin differs from this daily model publication")
    return origin_fields(current, expected)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("notice", type=Path)
    args = parser.parse_args(argv)
    try:
        origin = origin_for_push(args.notice, MARKET_DB_PATH)
    except (ValueError, OSError, LakeStoreOriginError) as error:
        print(f"daily machine publication stopped: {type(error).__name__}", file=sys.stderr)
        return 1
    print(json.dumps(origin, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
