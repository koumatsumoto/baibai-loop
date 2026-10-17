"""Retain bounded JPX source bundles before any business decoding."""

from __future__ import annotations

import gzip
import json
import sqlite3
import uuid
import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import cast

from baibai_engine.collection.jpx.models import (
    JPXAcquisitionError,
    JPXResponse,
    validate_jpx_url,
)

COLLECTION_NAMES = frozenset({"earnings_calendar", "regulations", "delistings", "capital_policy"})
INDEX_RESOURCE_NAME = "index"
Resource = tuple[str, str]


class JPXBundleError(ValueError):
    """A retained JPX bundle is missing, incomplete, or inconsistent."""


@dataclass(frozen=True, slots=True)
class JPXBundleReceipt:
    resource_name: str
    response: JPXResponse


@dataclass(frozen=True, slots=True)
class JPXSourceBundle:
    bundle_id: str
    collection_name: str
    asof_date: date
    started_at: datetime
    expected_resources: tuple[Resource, ...] | None
    completed_at: datetime | None
    responses: tuple[JPXBundleReceipt, ...]


def create_bundle(
    conn: sqlite3.Connection,
    collection_name: str,
    asof_date: date,
    started_at: datetime,
    expected_resources: Sequence[Resource] | None = None,
) -> str:
    _require_idle(conn)
    if collection_name not in COLLECTION_NAMES:
        raise JPXBundleError(f"unsupported JPX collection: {collection_name}")
    started = _require_utc(started_at, "bundle start")
    manifest = _validate_resources(expected_resources) if expected_resources is not None else None
    bundle_id = uuid.uuid4().hex
    try:
        conn.execute(
            "INSERT INTO jpx_source_bundles("
            "bundle_id, collection_name, asof_date, started_at_utc, started_on, "
            "expected_resources_json, completed_at_utc) VALUES (?, ?, ?, ?, ?, ?, NULL)",
            (
                bundle_id,
                collection_name,
                asof_date.isoformat(),
                _timestamp(started),
                started.date().isoformat(),
                _manifest_json(manifest),
            ),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return bundle_id


def declare_bundle_resources(
    conn: sqlite3.Connection, bundle_id: str, resources: Sequence[Resource]
) -> None:
    _require_idle(conn)
    manifest = _validate_resources(resources)
    try:
        conn.execute("BEGIN IMMEDIATE")
        bundle = read_bundle(conn, bundle_id)
        if bundle.completed_at is not None or bundle.expected_resources is not None:
            raise JPXBundleError("JPX bundle manifest cannot be changed")
        declared = dict(manifest)
        for receipt in bundle.responses:
            if declared.get(receipt.resource_name) != receipt.response.requested_url:
                raise JPXBundleError("JPX bundle manifest must include retained index exactly")
        updated = conn.execute(
            "UPDATE jpx_source_bundles SET expected_resources_json = ? "
            "WHERE bundle_id = ? AND expected_resources_json IS NULL "
            "AND completed_at_utc IS NULL",
            (_manifest_json(manifest), bundle_id),
        )
        if updated.rowcount != 1:
            raise JPXBundleError("JPX bundle manifest changed before declaration")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def retain_bundle_response(
    conn: sqlite3.Connection, bundle_id: str, resource_name: str, response: JPXResponse
) -> None:
    _require_idle(conn)
    try:
        conn.execute("BEGIN IMMEDIATE")
        bundle = read_bundle(conn, bundle_id)
        if bundle.completed_at is not None:
            raise JPXBundleError("completed JPX bundle is immutable")
        if not isinstance(cast(object, resource_name), str) or not resource_name:
            raise JPXBundleError("JPX resource name must be nonempty")
        _require_jpx_url(response.requested_url)
        received_at = _require_utc(response.received_at, "response capture")
        if received_at < bundle.started_at:
            raise JPXBundleError("JPX response capture precedes bundle start")
        if not isinstance(cast(object, response.content_type), str) or not isinstance(
            cast(object, response.body), bytes
        ):
            raise JPXBundleError("JPX response metadata or body is invalid")
        if bundle.expected_resources is None:
            if resource_name != INDEX_RESOURCE_NAME:
                raise JPXBundleError("only the index may be retained before manifest declaration")
        elif dict(bundle.expected_resources).get(resource_name) != response.requested_url:
            raise JPXBundleError("JPX response is not declared with this name and URL")
        if any(item.resource_name == resource_name for item in bundle.responses):
            raise JPXBundleError("JPX resource already retained in bundle")
        conn.execute(
            "INSERT INTO jpx_source_responses("
            "bundle_id, resource_name, requested_url, received_at_utc, received_on, "
            "content_type, body_gzip) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                bundle_id,
                resource_name,
                response.requested_url,
                _timestamp(received_at),
                received_at.date().isoformat(),
                response.content_type,
                gzip.compress(response.body, mtime=0),
            ),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def complete_bundle(conn: sqlite3.Connection, bundle_id: str) -> None:
    _require_idle(conn)
    try:
        conn.execute("BEGIN IMMEDIATE")
        bundle = read_bundle(conn, bundle_id)
        if bundle.completed_at is not None:
            raise JPXBundleError("completed JPX bundle is immutable")
        _require_exact_membership(bundle)
        completed_at = max(item.response.received_at for item in bundle.responses)
        updated = conn.execute(
            "UPDATE jpx_source_bundles SET completed_at_utc = ? "
            "WHERE bundle_id = ? AND completed_at_utc IS NULL",
            (_timestamp(completed_at), bundle_id),
        )
        if updated.rowcount != 1:
            raise JPXBundleError("JPX bundle completed before this update")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def read_bundle(conn: sqlite3.Connection, bundle_id: str) -> JPXSourceBundle:
    if conn.in_transaction:
        return _read_bundle(conn, bundle_id)
    conn.execute("BEGIN")
    try:
        bundle = _read_bundle(conn, bundle_id)
        conn.commit()
        return bundle
    except BaseException:
        conn.rollback()
        raise


def _read_bundle(conn: sqlite3.Connection, bundle_id: str) -> JPXSourceBundle:
    row = conn.execute(
        "SELECT bundle_id, collection_name, asof_date, started_at_utc, started_on, "
        "expected_resources_json, completed_at_utc "
        "FROM jpx_source_bundles WHERE bundle_id = ?",
        (bundle_id,),
    ).fetchone()
    if row is None:
        raise JPXBundleError(f"JPX bundle is not retained: {bundle_id}")
    try:
        if row[1] not in COLLECTION_NAMES:
            raise JPXBundleError("invalid JPX collection")
        asof_date = date.fromisoformat(row[2])
        started_at = _require_utc(datetime.fromisoformat(row[3]), "bundle start")
        if row[4] != started_at.date().isoformat():
            raise JPXBundleError("JPX bundle start day differs from UTC timestamp")
        expected = _decode_manifest(row[5])
        completed_at = (
            _require_utc(datetime.fromisoformat(row[6]), "bundle completion")
            if row[6] is not None
            else None
        )
        rows = conn.execute(
            "SELECT resource_name, requested_url, received_at_utc, received_on, "
            "content_type, body_gzip FROM jpx_source_responses "
            "WHERE bundle_id = ? ORDER BY resource_name",
            (bundle_id,),
        ).fetchall()
        responses: list[JPXBundleReceipt] = []
        for resource_name, url, received_text, received_on, content_type, body_gzip in rows:
            _require_jpx_url(url)
            received_at = _require_utc(datetime.fromisoformat(received_text), "response capture")
            if received_at < started_at or received_on != received_at.date().isoformat():
                raise JPXBundleError("JPX response capture day or order is invalid")
            if not isinstance(resource_name, str) or not resource_name:
                raise JPXBundleError("invalid JPX resource name")
            if not isinstance(content_type, str) or not isinstance(body_gzip, bytes):
                raise JPXBundleError("invalid JPX response payload")
            responses.append(
                JPXBundleReceipt(
                    resource_name,
                    JPXResponse(url, received_at, content_type, gzip.decompress(body_gzip)),
                )
            )
        bundle = JPXSourceBundle(
            str(row[0]),
            str(row[1]),
            asof_date,
            started_at,
            expected,
            completed_at,
            tuple(responses),
        )
        if expected is None:
            if any(item.resource_name != INDEX_RESOURCE_NAME for item in responses):
                raise JPXBundleError("undeclared JPX bundle contains a non-index resource")
        else:
            declared = dict(expected)
            if any(
                declared.get(item.resource_name) != item.response.requested_url
                for item in responses
            ):
                raise JPXBundleError("JPX response differs from declared resources")
        if completed_at is not None:
            _require_exact_membership(bundle)
            if completed_at != max(item.response.received_at for item in responses):
                raise JPXBundleError("JPX completion time differs from latest capture")
        return bundle
    except (TypeError, ValueError, gzip.BadGzipFile, EOFError, zlib.error) as exc:
        if isinstance(exc, JPXBundleError):
            raise
        raise JPXBundleError(f"corrupt retained JPX bundle: {bundle_id}") from exc


def read_complete_bundle(conn: sqlite3.Connection, bundle_id: str) -> JPXSourceBundle:
    bundle = read_bundle(conn, bundle_id)
    if bundle.completed_at is None:
        raise JPXBundleError("JPX bundle is incomplete")
    _require_exact_membership(bundle)
    return bundle


def _require_exact_membership(bundle: JPXSourceBundle) -> None:
    if bundle.expected_resources is None:
        raise JPXBundleError("JPX bundle has no declared resources")
    expected = set(bundle.expected_resources)
    actual = {(item.resource_name, item.response.requested_url) for item in bundle.responses}
    if expected != actual or len(expected) != len(bundle.responses):
        raise JPXBundleError("JPX bundle has missing or unexpected resources")


def _require_idle(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        raise JPXBundleError("JPX bundle write requires no pending transaction")


def _require_utc(value: datetime, label: str) -> datetime:
    if not isinstance(cast(object, value), datetime) or value.utcoffset() != timedelta(0):
        raise JPXBundleError(f"JPX {label} must be UTC")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="microseconds")


def _require_jpx_url(url: str) -> None:
    if not isinstance(url, str):
        raise JPXBundleError("JPX URL must be a string")
    try:
        validate_jpx_url(url)
    except JPXAcquisitionError as exc:
        raise JPXBundleError("JPX bundle URL must use https://www.jpx.co.jp/") from exc


def _validate_resources(resources: Sequence[Resource]) -> tuple[Resource, ...]:
    if isinstance(cast(object, resources), (str, bytes)) or not resources:
        raise JPXBundleError("JPX resource manifest must be nonempty")
    result: list[Resource] = []
    for item in resources:
        if not isinstance(cast(object, item), (tuple, list)) or len(item) != 2:
            raise JPXBundleError("JPX resource must be a name and URL pair")
        name, url = item
        if not isinstance(cast(object, name), str) or not name:
            raise JPXBundleError("JPX resource name must be nonempty")
        _require_jpx_url(url)
        result.append((name, url))
    if len({name for name, _ in result}) != len(result):
        raise JPXBundleError("JPX resource names must be unique")
    if len({url for _, url in result}) != len(result):
        raise JPXBundleError("JPX resource URLs must be unique")
    return tuple(result)


def _manifest_json(resources: tuple[Resource, ...] | None) -> str | None:
    return json.dumps(resources, ensure_ascii=False, separators=(",", ":")) if resources else None


def _decode_manifest(value: str | None) -> tuple[Resource, ...] | None:
    if value is None:
        return None
    payload = json.loads(value)
    if not isinstance(payload, list):
        raise JPXBundleError("invalid JPX resource manifest")
    return _validate_resources(payload)
