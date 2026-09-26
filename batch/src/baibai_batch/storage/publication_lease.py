"""One conditional R2 lease coordinating production publication across runners."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import tempfile
import uuid
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

from .lake_publish import Boto3R2Store, LakeCASConflict, LakePublishError, ObjectStore

PUBLICATION_LEASE_KEY = "coordination/publication-lease.json"
DEFAULT_STORES_BUCKET = "baibai-stores"
LEASE_TTL = timedelta(minutes=120)
_PURPOSES = ("daily", "tradingview", "materialize", "operator")
_OWNER = re.compile(r"(?:github:[0-9]+:[0-9]+|local:[0-9a-f]{32})\Z")
_WIRE_KEYS = {"state", "owner", "purpose", "acquired_at_utc", "expires_at_utc"}
_HANDLE_KEYS = {"owner", "purpose", "etag", "acquired_at_utc", "expires_at_utc"}


class LeaseError(RuntimeError):
    """Remote state, local handle, or publication outcome cannot be trusted."""


class LeaseBusy(LeaseError):
    def __init__(self, lease: PublicationLease | None = None) -> None:
        self.lease = lease
        super().__init__("publication lease busy")


@dataclass(frozen=True, slots=True)
class PublicationLease:
    state: Literal["held", "released"]
    owner: str
    purpose: str
    acquired_at_utc: datetime
    expires_at_utc: datetime


@dataclass(frozen=True, slots=True)
class PublicationLeaseHandle:
    owner: str
    purpose: str
    etag: str
    acquired_at_utc: datetime
    expires_at_utc: datetime


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise LeaseError("invalid publication lease timestamp")
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        raise LeaseError("invalid publication lease timestamp") from None
    if result.tzinfo is None or result.utcoffset() != timedelta(0):
        raise LeaseError("publication lease timestamp must be UTC")
    return result.astimezone(UTC)


def _validate(lease: PublicationLease) -> PublicationLease:
    if lease.state not in ("held", "released"):
        raise LeaseError("invalid publication lease state")
    if _OWNER.fullmatch(lease.owner) is None or len(lease.owner) > 80:
        raise LeaseError("invalid publication lease owner")
    if lease.purpose not in _PURPOSES:
        raise LeaseError("invalid publication lease purpose")
    if lease.expires_at_utc - lease.acquired_at_utc != LEASE_TTL:
        raise LeaseError("invalid publication lease TTL")
    return lease


def _decode(payload: bytes) -> PublicationLease:
    try:
        value = json.loads(payload)
        if not isinstance(value, dict) or set(value) != _WIRE_KEYS:
            raise LeaseError("invalid publication lease fields")
        if any(not isinstance(value[key], str) for key in _WIRE_KEYS):
            raise LeaseError("invalid publication lease value")
        lease = PublicationLease(
            state=value["state"],
            owner=value["owner"],
            purpose=value["purpose"],
            acquired_at_utc=_timestamp(value["acquired_at_utc"]),
            expires_at_utc=_timestamp(value["expires_at_utc"]),
        )
        return _validate(lease)
    except (ValueError, UnicodeDecodeError) as exc:
        raise LeaseError("invalid publication lease JSON") from exc


def _encode(lease: PublicationLease) -> bytes:
    return json.dumps(
        {
            "state": lease.state,
            "owner": lease.owner,
            "purpose": lease.purpose,
            "acquired_at_utc": lease.acquired_at_utc.isoformat(),
            "expires_at_utc": lease.expires_at_utc.isoformat(),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _generation(store: ObjectStore) -> tuple[PublicationLease, bytes, str] | None:
    before = store.head(PUBLICATION_LEASE_KEY)
    if before is None:
        return None
    if not before.etag:
        raise LeaseError("invalid publication lease object")
    payload = store.get_bytes(PUBLICATION_LEASE_KEY)
    after = store.head(PUBLICATION_LEASE_KEY)
    if after is None or after.etag != before.etag:
        raise LeaseError("publication lease changed during read")
    return _decode(payload), payload, after.etag


def _put(store: ObjectStore, payload: bytes, *, etag: str | None) -> str:
    descriptor, name = tempfile.mkstemp(prefix="baibai-lease-", suffix=".json")
    path = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
        result = store.put_file(
            PUBLICATION_LEASE_KEY,
            path,
            sha256=hashlib.sha256(payload).hexdigest(),
            content_md5=base64.b64encode(
                hashlib.md5(payload, usedforsecurity=False).digest()
            ).decode(),
            content_type="application/json",
            if_match=etag,
            if_none_match=etag is None,
        )
        return result.etag
    finally:
        path.unlink(missing_ok=True)


def _owner() -> str:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        run_id = os.environ.get("GITHUB_RUN_ID", "")
        attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "")
        owner = f"github:{run_id}:{attempt}"
        if _OWNER.fullmatch(owner) is None or len(owner) > 80:
            raise LeaseError("invalid GitHub runner identity")
        return owner
    return f"local:{uuid.uuid4().hex}"


def acquire(
    store: ObjectStore, purpose: str, *, now: datetime | None = None
) -> tuple[PublicationLeaseHandle, bool]:
    if purpose not in _PURPOSES:
        raise LeaseError("invalid publication lease purpose")
    owner = _owner()
    current = _generation(store)
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    expired_previous = False
    if current is not None:
        previous, _, etag = current
        if previous.state == "held" and previous.expires_at_utc > moment:
            raise LeaseBusy(previous)
        expired_previous = previous.state == "held"
    else:
        etag = None
    lease = _validate(PublicationLease("held", owner, purpose, moment, moment + LEASE_TTL))
    payload = _encode(lease)
    try:
        acquired_etag = _put(store, payload, etag=etag)
    except (LakePublishError, LakeCASConflict):
        try:
            observed = _generation(store)
        except (LakePublishError, LeaseError) as exc:
            raise LeaseError("publication lease acquire outcome unknown") from exc
        if observed is None:
            raise LeaseError("publication lease acquire outcome unknown") from None
        other, remote_payload, acquired_etag = observed
        if remote_payload != payload:
            raise LeaseBusy(other) from None
    if not acquired_etag:
        raise LeaseError("publication lease acquired without ETag")
    return (
        PublicationLeaseHandle(
            lease.owner, lease.purpose, acquired_etag, lease.acquired_at_utc, lease.expires_at_utc
        ),
        expired_previous,
    )


def release(store: ObjectStore, handle: PublicationLeaseHandle) -> None:
    if not handle.etag or len(handle.etag) > 256:
        raise LeaseError("invalid publication lease handle ETag")
    held = _validate(
        PublicationLease(
            "held", handle.owner, handle.purpose, handle.acquired_at_utc, handle.expires_at_utc
        )
    )
    payload = _encode(replace(held, state="released"))
    try:
        _put(store, payload, etag=handle.etag)
    except (LakePublishError, LakeCASConflict):
        try:
            observed = _generation(store)
        except (LakePublishError, LeaseError) as exc:
            raise LeaseError("publication lease release outcome unknown") from exc
        if observed is None:
            raise LeaseError("publication lease release outcome unknown") from None
        current, remote_payload, _ = observed
        if remote_payload == payload:
            return
        if current.owner != handle.owner:
            raise LeaseError("publication lease release conflicted with another owner") from None
        raise LeaseError("publication lease release did not complete") from None


def _handle_payload(handle: PublicationLeaseHandle) -> bytes:
    return json.dumps(
        {
            "owner": handle.owner,
            "purpose": handle.purpose,
            "etag": handle.etag,
            "acquired_at_utc": handle.acquired_at_utc.isoformat(),
            "expires_at_utc": handle.expires_at_utc.isoformat(),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _save_handle(path: Path, handle: PublicationLeaseHandle) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(_handle_payload(handle))
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, path)  # Atomic create, never overwrite an earlier handle.
    finally:
        temporary.unlink(missing_ok=True)


def _load_handle(path: Path) -> PublicationLeaseHandle:
    try:
        value = json.loads(path.read_bytes())
        if not isinstance(value, dict) or set(value) != _HANDLE_KEYS:
            raise LeaseError("invalid publication lease handle fields")
        if any(not isinstance(value[key], str) for key in _HANDLE_KEYS):
            raise LeaseError("invalid publication lease handle value")
        handle = PublicationLeaseHandle(
            value["owner"],
            value["purpose"],
            value["etag"],
            _timestamp(value["acquired_at_utc"]),
            _timestamp(value["expires_at_utc"]),
        )
        if not handle.etag or len(handle.etag) > 256:
            raise LeaseError("invalid publication lease handle ETag")
        _validate(
            PublicationLease(
                "held", handle.owner, handle.purpose, handle.acquired_at_utc, handle.expires_at_utc
            )
        )
        return handle
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise LeaseError("invalid publication lease handle") from exc


def _load_project_env(module_path: Path) -> None:
    # This module lives at batch/src/baibai_batch/storage. Keep the optional
    # local credential file tied to the checkout, never the caller's cwd.
    load_dotenv(module_path.resolve().parents[4] / ".env", override=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="publication_lease")
    commands = parser.add_subparsers(dest="command", required=True)
    take = commands.add_parser("acquire")
    take.add_argument("--purpose", choices=_PURPOSES, required=True)
    take.add_argument("--handle", type=Path, required=True)
    give = commands.add_parser("release")
    give.add_argument("--handle", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "acquire" and args.handle.exists():
        print("publication lease: handle already exists", file=sys.stderr)
        return 1
    try:
        handle = _load_handle(args.handle) if args.command == "release" else None
        _load_project_env(Path(__file__))
        store = Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or DEFAULT_STORES_BUCKET)
        if args.command == "acquire":
            acquired, expired = acquire(store, args.purpose)
            try:
                _save_handle(args.handle, acquired)
            except OSError:
                with suppress(LakePublishError, LeaseError):
                    release(store, acquired)
                raise LeaseError("publication lease handle could not be saved") from None
            output = os.environ.get("GITHUB_OUTPUT")
            if output:
                try:
                    with Path(output).open("a", encoding="utf-8") as destination:
                        destination.write("acquired=true\n")
                except OSError:
                    with suppress(LakePublishError, LeaseError):
                        release(store, acquired)
                    raise LeaseError("publication lease output could not be saved") from None
            print(
                f"publication lease: acquired purpose={acquired.purpose} "
                f"expires_at={acquired.expires_at_utc.isoformat()} "
                f"expired_previous={str(expired).lower()}"
            )
        else:
            assert handle is not None
            release(store, handle)
            args.handle.unlink()
            print(f"publication lease: released purpose={handle.purpose}")
        return 0
    except LeaseBusy as exc:
        current = exc.lease
        if current is not None:
            print(
                f"publication lease: busy purpose={current.purpose} owner={current.owner} "
                f"expires_at={current.expires_at_utc.isoformat()}",
                file=sys.stderr,
            )
        else:
            print("publication lease: busy", file=sys.stderr)
        return 3
    except (LakePublishError, LeaseError, OSError) as exc:
        # The exception text of an R2 client may contain request details. Report only
        # this fixed diagnostic at the CLI boundary.
        print(f"publication lease: failure ({type(exc).__name__})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
