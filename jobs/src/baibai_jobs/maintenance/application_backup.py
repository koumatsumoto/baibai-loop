"""Appの更新前世代を内容別に保持し、同日の既存backupを置換しない。"""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from baibai_engine.storage.objects.r2 import Boto3R2Store, LakePublishError, ObjectStore


def preserve_application(
    store: ObjectStore, *, expected_etag: str, directory: Path, day: str
) -> str:
    expected = expected_etag.strip('"')
    if re.fullmatch(r"[0-9a-f]{32}", expected) is None or re.fullmatch(r"[0-9]{8}", day) is None:
        raise ValueError("application backup requires a single-part generation and JST date")
    source = store.head("baibai.sqlite")
    if source is None or source.etag != expected:
        raise ValueError("application generation changed before backup")
    body = directory / "prior-application.sqlite"
    store.download_file("baibai.sqlite", body, expect_bytes=source.size)
    with body.open("rb") as stream:
        md5 = hashlib.file_digest(stream, "md5")
    if md5.hexdigest() != expected or store.head("baibai.sqlite") != source:
        raise ValueError("application generation changed during backup")
    with body.open("rb") as stream:
        sha = hashlib.file_digest(stream, "sha256").hexdigest()
    key = f"baibai.sqlite.bak-{day}-{sha}"
    prior = store.head(key)
    if prior is None:
        legacy_key = f"baibai.sqlite.bak-{day}"
        legacy = store.head(legacy_key)
        if legacy is not None and legacy.etag == expected and legacy.size == source.size:
            key, prior = legacy_key, legacy
    if prior is None:
        prior = store.put_file(
            key,
            body,
            sha256=sha,
            content_md5=base64.b64encode(md5.digest()).decode(),
            content_type="application/vnd.sqlite3",
            if_none_match=True,
        )
    if prior.size != source.size:
        raise ValueError("application backup size differs")
    readback = directory / "backup-readback.sqlite"
    store.download_file(key, readback, expect_bytes=source.size)
    with readback.open("rb") as stream:
        received_sha = hashlib.file_digest(stream, "sha256").hexdigest()
    if received_sha != sha or store.head(key) != prior:
        raise ValueError("application backup readback differs")
    return key


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--expected-etag", required=True)
    args = parser.parse_args()
    env = dict(os.environ)
    env["R2_STOP_ON_REJECTION"] = "1"
    store = Boto3R2Store(bucket=args.bucket, env=env, verify_put_with_head=False)
    try:
        with tempfile.TemporaryDirectory(prefix="application-backup-") as temporary:
            key = preserve_application(
                store,
                expected_etag=args.expected_etag,
                directory=Path(temporary),
                day=datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y%m%d"),
            )
        print(f"application backup verified: {key}")
        return 0
    except (LakePublishError, OSError, ValueError):
        print("application backup failed; application was not uploaded")
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
