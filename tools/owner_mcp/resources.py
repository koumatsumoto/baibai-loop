"""調査工程へ型付きcatalog・bounded page・固定documentを共通3入口で見せる。"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable
from hashlib import sha256
from typing import Any

from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, ValidationError

from baibai_engine.appdb.json import canonical_json
from baibai_engine.foundation.redaction import redact_credentials
from baibai_engine.read_api.calibration import SnapshotChangedError
from baibai_engine.screening.run_store import RunStoreAmbiguousError
from tools.l1_mcp.contract import LIMITS, wire

from .catalog import descriptor, resources
from .reader import OwnerError
from .resource_models import Cursor, ResourceRef, iso_day, iso_instant
from .resource_types import Page, Paths, ResourceSpec

SUCCESS_TEXT = "成功。固定referenceと入力basisを確認してください。"


def result_size(payload: dict[str, Any]) -> int:
    result = CallToolResult(
        content=[TextContent(type="text", text=SUCCESS_TEXT)], structured_content=payload
    )
    return len(wire(result.model_dump(mode="json", by_alias=True, exclude_none=True)))


def public(value: Any) -> Any:
    if isinstance(value, str):
        return redact_credentials(value)
    if isinstance(value, dict):
        return {key: public(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [public(item) for item in value]
    return value


def parsed(model: type[BaseModel], value: dict[str, Any]) -> dict[str, Any]:
    try:
        return model.model_validate(value).model_dump(mode="json", by_alias=True, exclude_none=True)
    except (ValidationError, ValueError) as exc:
        raise OwnerError("INVALID_ARGUMENT") from exc


def encode_cursor(
    resource_id: str, filters: dict[str, Any], after: list[str | int | float], meta: dict[str, Any]
) -> str:
    return base64.urlsafe_b64encode(
        wire(
            {
                "resource_id": resource_id,
                "filters": filters,
                "after": after,
                "snapshot_token": meta.get("snapshot_token"),
            }
        )
    ).decode()


def decode_cursor(raw: str) -> Cursor:
    try:
        if len(raw) > LIMITS.parameter_bytes * 2:
            raise ValueError("cursor too large")
        return Cursor.model_validate(
            json.loads(base64.b64decode(raw, altchars=b"-_", validate=True))
        )
    except (ValueError, binascii.Error, ValidationError) as exc:
        raise OwnerError("INVALID_ARGUMENT") from exc


class Resources:
    def __init__(self, paths: Paths | None = None) -> None:
        self.paths = paths or Paths()
        self.specs = resources()

    def spec(self, resource_id: str) -> ResourceSpec:
        if resource_id not in self.specs:
            raise OwnerError("INVALID_ARGUMENT")
        return self.specs[resource_id]

    def catalog(
        self, resource_id: str | None = None, layer: str | None = None, domain: str | None = None
    ) -> dict[str, Any]:
        if resource_id is not None:
            if layer is not None or domain is not None:
                raise OwnerError("INVALID_ARGUMENT")
            return descriptor(self.spec(resource_id), detail=True)
        if layer not in {None, "L1", "L2", "L3"} or domain not in {
            None,
            *(s.domain for s in self.specs.values()),
        }:
            raise OwnerError("INVALID_ARGUMENT")
        return {
            "resources": [
                descriptor(spec)
                for spec in self.specs.values()
                if layer in (None, spec.layer) and domain in (None, spec.domain)
            ]
        }

    def _read(self, action: Callable[[], Page]) -> Page:
        try:
            return action()
        except FileNotFoundError as exc:
            raise OwnerError("SOURCE_UNAVAILABLE") from exc
        except SnapshotChangedError as exc:
            raise OwnerError("REFERENCE_MISMATCH") from exc
        except RunStoreAmbiguousError as exc:
            raise OwnerError("AMBIGUOUS_SELECTION") from exc

    def get(
        self,
        resource_id: str,
        selector: dict[str, Any] | None = None,
        resource_ref: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        spec = self.spec(resource_id)
        if spec.get is None:
            raise OwnerError(
                "INVALID_ARGUMENT",
                "market lakeはl1_resolve_current / l1_describe_dataset / "
                "l1_queryを使用してください。",
            )
        if selector is not None and resource_ref is not None:
            raise OwnerError("INVALID_ARGUMENT")
        reference = None
        if resource_ref is not None:
            reference = parsed(ResourceRef, resource_ref)
            if reference["resource_id"] != resource_id:
                raise OwnerError("INVALID_ARGUMENT")
            selection = parsed(spec.identity, reference["identity"])
        else:
            selection = parsed(spec.selector, selector or {})
        get = spec.get
        result = self._read(lambda: get(self.paths, selection))
        if not result.records:
            raise OwnerError("SOURCE_UNAVAILABLE")
        row = result.records[0]
        normalized_payload = json.loads(canonical_json(row.payload))
        payload = public(normalized_payload)
        digest = sha256(canonical_json(payload).encode("utf-8")).hexdigest()
        identity = parsed(spec.identity, row.identity)
        if reference is not None and (
            reference["sha256"] != digest or reference["identity"] != identity
        ):
            raise OwnerError("REFERENCE_MISMATCH")
        return {
            "resource_id": resource_id,
            "resource_ref": {"resource_id": resource_id, "identity": identity, "sha256": digest},
            "payload": payload,
            "meta": {
                "authority": spec.authority,
                "basis": "stored",
                "payload_scope": "full",
                "redacted": payload != normalized_payload,
                **result.meta,
            },
        }

    def list(
        self,
        resource_id: str,
        filters: dict[str, Any] | None = None,
        limit: int = 200,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        spec = self.spec(resource_id)
        if spec.get is None:
            raise OwnerError(
                "INVALID_ARGUMENT",
                "market lakeはl1_resolve_current / l1_describe_dataset / "
                "l1_queryを使用してください。",
            )
        if spec.page is None or type(limit) is not int or not 1 <= limit <= LIMITS.max_rows:
            raise OwnerError("INVALID_ARGUMENT")
        decoded = decode_cursor(cursor) if cursor is not None else None
        if decoded is not None and decoded.resource_id != resource_id:
            raise OwnerError("INVALID_ARGUMENT")
        normalized = parsed(
            spec.filters,
            decoded.filters if decoded is not None and filters is None else (filters or {}),
        )
        after = None
        if decoded is not None:
            if parsed(spec.filters, decoded.filters) != normalized or len(decoded.after) != len(
                spec.sort
            ):
                raise OwnerError("INVALID_ARGUMENT")
            after = decoded.after
            for name, value in zip(spec.sort, after, strict=True):
                if name in {"ordinal", "append_seq"}:
                    if type(value) is not int or value < 0:
                        raise OwnerError("INVALID_ARGUMENT")
                elif name in {
                    "published_at",
                    "reviewed_at",
                    "started_at",
                    "finished_at",
                    "created_at",
                }:
                    if type(value) not in (int, float):
                        raise OwnerError("INVALID_ARGUMENT")
                elif not isinstance(value, str):
                    raise OwnerError("INVALID_ARGUMENT")
                elif name in {
                    "observed_at",
                    "asof",
                    "asof_date",
                    "as_of",
                    "period_end_date",
                    "snapshot_month_end",
                    "vintage_at",
                    "run_at",
                }:
                    try:
                        (iso_instant if name in {"vintage_at", "run_at"} else iso_day)(value)
                    except ValueError as exc:
                        raise OwnerError("INVALID_ARGUMENT") from exc
        query = dict(normalized)
        if decoded is not None and decoded.snapshot_token is not None:
            if not resource_id.startswith("screening.calibration."):
                raise OwnerError("INVALID_ARGUMENT")
            if query.get("snapshot_token") not in (None, decoded.snapshot_token):
                raise OwnerError("INVALID_ARGUMENT")
            query["snapshot_token"] = decoded.snapshot_token
        page_reader = spec.page
        page = self._read(lambda: page_reader(self.paths, query, after, limit + 1))
        response: dict[str, Any] = {
            "resource_id": resource_id,
            "items": [],
            "returned_count": 0,
            "next_cursor": None,
            "meta": {
                "authority": spec.authority,
                "basis": "stored",
                "consistency": "per_call",
                "payload_scope": spec.list_shape,
                "redacted": False,
                **page.meta,
            },
        }
        for index, row in enumerate(page.records[:limit]):
            identity = {
                key: value
                for key, value in row.identity.items()
                if key in spec.selector.model_fields
            }
            # Reading exact selector deliberately excludes the rule revision from its identity.
            normalized_payload = json.loads(canonical_json(row.payload))
            payload = public(normalized_payload)
            item = {"selector": identity, "payload": payload}
            previous_cursor = response["next_cursor"]
            response["items"].append(item)
            response["returned_count"] += 1
            response["meta"]["redacted"] |= payload != normalized_payload
            response["next_cursor"] = (
                encode_cursor(resource_id, normalized, row.key, page.meta)
                if index + 1 < len(page.records)
                else None
            )
            if result_size(response) > LIMITS.result_bytes:
                response["items"].pop()
                response["returned_count"] -= 1
                if not response["items"]:
                    raise OwnerError("RESULT_TOO_LARGE")
                response["next_cursor"] = previous_cursor
                break
        return response
