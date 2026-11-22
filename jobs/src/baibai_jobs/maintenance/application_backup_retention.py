"""全ページを読めたAppバックアップ一覧から、保持期限を過ぎた通常名だけを出力する。"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

_PREFIX = "baibai.sqlite.bak-"
_NAME = re.compile(re.escape(_PREFIX) + r"([0-9]{8})(?:-[0-9a-f]{64})?")


def _list_backup_keys(bucket: str, endpoint: str) -> set[str]:
    # Auto-pagination drops the individual page counts and truncation flags.
    command = [
        "aws",
        "s3api",
        "list-objects-v2",
        "--no-paginate",
        "--output",
        "json",
        "--bucket",
        bucket,
        "--prefix",
        _PREFIX,
        "--endpoint-url",
        endpoint,
    ]
    keys: set[str] = set()
    cursors: set[str] = set()
    cursor: str | None = None
    while True:
        # Keep the opaque token JSON data, not an AWS CLI parameter-file reference.
        continuation = (
            ["--cli-input-json", json.dumps({"ContinuationToken": cursor})]
            if cursor is not None
            else []
        )
        page = json.loads(subprocess.check_output([*command, *continuation]))
        if (
            not isinstance(page, dict)
            or page.get("Prefix") != _PREFIX
            or page.get("Name", bucket) != bucket
            or page.get("CommonPrefixes", []) != []
            or page.get("ContinuationToken", cursor) != cursor
            or "NextToken" in page
        ):
            raise ValueError("unexpected application backup list response")
        # An empty S3 page can omit Contents, but still carries a zero KeyCount.
        entries = page.get("Contents", [])
        if (
            not isinstance(entries, list)
            or type(page.get("KeyCount")) is not int
            or page["KeyCount"] != len(entries)
        ):
            raise ValueError("application backup list count/contents mismatch")
        for entry in entries:
            key = entry.get("Key") if isinstance(entry, dict) else None
            if not isinstance(key, str) or not key.startswith(_PREFIX) or key in keys:
                raise ValueError("invalid, foreign, or repeated key in application backup listing")
            keys.add(key)
        if page.get("IsTruncated") is False and "NextContinuationToken" not in page:
            return keys
        following = page.get("NextContinuationToken")
        if (
            page.get("IsTruncated") is not True
            or not isinstance(following, str)
            or not following
            or following in cursors
        ):
            raise ValueError("incomplete application backup listing")
        cursors.add(following)
        cursor = following


def expired_backup_keys(keys: set[str], *, as_of: datetime) -> list[str]:
    today = as_of.astimezone(ZoneInfo("Asia/Tokyo")).date()
    dates: set[date] = set()
    expired: list[str] = []
    for key in sorted(keys, reverse=True):
        match = _NAME.fullmatch(key)
        try:
            day = date.fromisoformat(match[1]) if match is not None else None
        except ValueError:
            day = None
        if day is None or day > today:
            print(
                f"leaving an unrecognised key under the application backup prefix: {key!a}",
                file=sys.stderr,
            )
            continue
        dates.add(day)
        if len(dates) > 14:
            expired.append(key)
    return expired


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--endpoint-url", required=True)
    args = parser.parse_args()
    as_of = datetime.now(UTC)
    try:
        keys = _list_backup_keys(args.bucket, args.endpoint_url)
    except (OSError, subprocess.CalledProcessError):
        print("application backup key listing failed", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"invalid application backup key listing: {exc}", file=sys.stderr)
        return 1
    for key in expired_backup_keys(keys, as_of=as_of):
        print(key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
