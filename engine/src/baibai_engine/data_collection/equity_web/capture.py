"""Private, versioned local handoff of received public HTML originals."""

from __future__ import annotations

import base64
import binascii
import json
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from baibai_engine.data_collection.equity_web.models import EquityWebSourceResponse
from baibai_engine.data_collection.equity_web.receipts import validate_response

CAPTURE_FORMAT = "equity-web-source-capture-v1"
_RESPONSE_FIELDS = frozenset(
    {
        "receipt_id",
        "attempt_id",
        "source",
        "requested_ticker",
        "role",
        "parent_receipt_id",
        "requested_url",
        "received_at_utc",
        "selected_encoding",
        "body_base64",
    }
)


@dataclass(frozen=True, slots=True)
class EquityWebCapture:
    responses: tuple[EquityWebSourceResponse, ...]
    selected_attempt_ids: tuple[str, ...]


class LegacyCaptureError(ValueError):
    """A normalized observation stage cannot be imported as originals."""


def _validate(capture: EquityWebCapture) -> None:
    receipts: dict[str, EquityWebSourceResponse] = {}
    roles: set[tuple[str, str]] = set()
    attempts: set[str] = set()
    for response in capture.responses:
        validate_response(response)
        if response.receipt_id in receipts or (response.attempt_id, response.role) in roles:
            raise ValueError("duplicate original response")
        receipts[response.receipt_id] = response
        roles.add((response.attempt_id, response.role))
        attempts.add(response.attempt_id)
    for response in capture.responses:
        if response.parent_receipt_id is not None:
            parent = receipts.get(response.parent_receipt_id)
            if parent is None or (
                parent.attempt_id,
                parent.source,
                parent.requested_ticker,
                parent.role,
            ) != (response.attempt_id, response.source, response.requested_ticker, "page"):
                raise ValueError("iframe parent is absent or mismatched")
    selected: set[str] = set()
    for attempt_id in capture.selected_attempt_ids:
        if not isinstance(attempt_id, str):
            raise ValueError("selected attempt ID must be a canonical UUID")
        if attempt_id in selected:
            raise ValueError("duplicate selected attempt ID")
        selected.add(attempt_id)
        try:
            identity = uuid.UUID(attempt_id)
        except ValueError as exc:
            raise ValueError("selected attempt ID must be a canonical UUID") from exc
        if str(identity) != attempt_id or identity.int == 0 or attempt_id not in attempts:
            raise ValueError("selected attempt ID is invalid or absent")


def save_capture(path: Path, capture: EquityWebCapture) -> None:
    """Create a private file atomically without replacing an earlier capture."""
    _validate(capture)
    payload = {
        "format": CAPTURE_FORMAT,
        "responses": [
            {
                "receipt_id": response.receipt_id,
                "attempt_id": response.attempt_id,
                "source": response.source,
                "requested_ticker": response.requested_ticker,
                "role": response.role,
                "parent_receipt_id": response.parent_receipt_id,
                "requested_url": response.requested_url,
                "received_at_utc": response.received_at_utc.isoformat(timespec="microseconds"),
                "selected_encoding": response.selected_encoding,
                "body_base64": base64.b64encode(response.body).decode("ascii"),
            }
            for response in capture.responses
        ],
        "selected_attempt_ids": list(capture.selected_attempt_ids),
    }
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, allow_nan=False)
        path.hardlink_to(temporary)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate capture JSON field")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid capture JSON constant: {value}")


def load_capture(path: Path) -> EquityWebCapture:
    """Validate exact metadata and base64 bytes; never infer transport outcomes."""
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid equity web capture JSON") from exc
    if isinstance(payload, dict) and payload.get("format") == "external-assessment-staging-v1":
        raise LegacyCaptureError("normalized observation stage is not a source capture")
    if (
        not isinstance(payload, dict)
        or set(payload) != {"format", "responses", "selected_attempt_ids"}
        or payload["format"] != CAPTURE_FORMAT
        or not isinstance(payload["responses"], list)
        or not isinstance(payload["selected_attempt_ids"], list)
    ):
        raise ValueError("invalid equity web capture format")
    responses: list[EquityWebSourceResponse] = []
    for item in payload["responses"]:
        if not isinstance(item, dict) or set(item) != _RESPONSE_FIELDS:
            raise ValueError("invalid equity web capture response")
        if not isinstance(item["received_at_utc"], str) or not isinstance(item["body_base64"], str):
            raise ValueError("invalid equity web capture response encoding")
        try:
            body = base64.b64decode(item["body_base64"], validate=True)
            if base64.b64encode(body).decode("ascii") != item["body_base64"]:
                raise ValueError("noncanonical capture body encoding")
            received = datetime.fromisoformat(item["received_at_utc"])
        except (binascii.Error, ValueError) as exc:
            raise ValueError("invalid equity web capture response encoding") from exc
        responses.append(
            EquityWebSourceResponse(
                receipt_id=item["receipt_id"],
                attempt_id=item["attempt_id"],
                source=item["source"],
                requested_ticker=item["requested_ticker"],
                role=item["role"],
                parent_receipt_id=item["parent_receipt_id"],
                requested_url=item["requested_url"],
                received_at_utc=received,
                selected_encoding=item["selected_encoding"],
                body=body,
            )
        )
    capture = EquityWebCapture(tuple(responses), tuple(payload["selected_attempt_ids"]))
    _validate(capture)
    return capture
