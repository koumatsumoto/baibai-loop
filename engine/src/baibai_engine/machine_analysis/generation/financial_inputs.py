"""財務の固定入力を共有保持する。原本・日次head・再生成可能な観測は複写しない。"""

from __future__ import annotations

import base64
import hashlib
import json
import tempfile
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, JsonValue, model_validator

from baibai_engine.machine_analysis.generation.contracts import Digest
from baibai_engine.primitives.json import canonical_json
from baibai_engine.storage.objects.r2 import LakePublishError, ObjectStore

INPUT_FORMAT = "jquants-financial-input-v2"
INPUT_PREFIX = "fixed-inputs/jquants-financial/v1/"


class _Selection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    format: Literal["jquants-financial-selection-v1"] = "jquants-financial-selection-v1"
    start: date
    end: date
    months: dict[str, Digest]

    @model_validator(mode="after")
    def bounded_months(self) -> _Selection:
        if self.start > self.end:
            raise ValueError("invalid fixed financial selection interval")
        for month in self.months:
            first = date.fromisoformat(month + "-01")
            if first.strftime("%Y-%m") != month or not (
                self.start.strftime("%Y-%m") <= month <= self.end.strftime("%Y-%m")
            ):
                raise ValueError("financial month is outside its selection")
        return self


def _key(kind: str, digest: str) -> str:
    # Validate saved locators before composing a key; never accept caller paths.
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("invalid fixed financial object digest")
    return INPUT_PREFIX + kind + "/" + digest + ".json"


def _read(store: ObjectStore, key: str, digest: str) -> dict[str, Any]:
    if store.head(key) is None:
        raise LakePublishError("fixed financial input object is unavailable")
    payload = store.get_bytes(key)
    if hashlib.sha256(payload).hexdigest() != digest:
        raise LakePublishError("fixed financial input object failed integrity")
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("fixed financial input object must be an object")
    return value


def _retain(store: ObjectStore, kind: str, value: dict[str, Any], directory: Path) -> str:
    payload = canonical_json(value).encode()
    digest = hashlib.sha256(payload).hexdigest()
    key = _key(kind, digest)
    existing = store.head(key)
    if existing is not None:
        if existing.size != len(payload) or existing.metadata.get("sha256") != digest:
            raise LakePublishError("fixed financial input object identity conflicts")
        return digest
    path = directory / (digest + ".json")
    path.write_bytes(payload)
    try:
        store.put_file(
            key,
            path,
            sha256=digest,
            content_md5=base64.b64encode(
                hashlib.md5(payload, usedforsecurity=False).digest()
            ).decode(),
            content_type="application/json",
            if_none_match=True,
        )
    except LakePublishError:
        # A concurrent identical write or a lost acknowledgement is successful
        # only after the exact immutable bytes are confirmed. Never mint a new ID.
        if store.head(key) is None:
            raise
        _read(store, key, digest)
    else:
        _read(store, key, digest)
    return digest


def retain_financial_input(store: ObjectStore, selected: dict[str, Any]) -> dict[str, JsonValue]:
    """Write only changed monthly metadata; the saved identity has bounded size.

    These objects are reconstruction inputs retained independently of runs cache.
    No latest pointer or automatic expiry is created here.
    """
    scopes, receipts = selected["scopes"], selected["receipts"]
    days = sorted(scopes)
    if not days:
        raise ValueError("financial input selection requires its date interval")
    months: dict[str, dict[str, Any]] = {}
    for day in days:
        if not (scope := scopes[day]):
            continue  # The root's closed interval fixes missing days explicitly.
        month = day[:7]
        block = months.setdefault(
            month,
            {"format": "jquants-financial-month-v1", "month": month, "scopes": {}, "receipts": {}},
        )
        block["scopes"][day] = scope
        for field in ("adopted", "failure", "held"):
            if identity := scope.get(field):
                block["receipts"][identity] = receipts[identity]
    with tempfile.TemporaryDirectory(prefix=".financial-input-") as temporary:
        directory = Path(temporary)
        identities = {
            month: _retain(store, "months", block, directory) for month, block in months.items()
        }
        root = _Selection(start=days[0], end=days[-1], months=identities)
        digest = _retain(store, "selections", root.model_dump(mode="json"), directory)
    return {
        "format": INPUT_FORMAT,
        "head_sha256": selected["head_sha256"],
        "as_of": selected["as_of"],
        "selection_sha256": digest,
        "summary": selected["summary"],
    }


def _bounded_input(
    scopes: dict[str, Any], receipts: dict[str, Any], days: Sequence[date]
) -> dict[str, Any]:
    selected = {day.isoformat(): scopes[day.isoformat()] for day in days}
    wanted = {
        identity
        for scope in selected.values()
        for field in ("adopted", "failure", "held")
        if (identity := scope.get(field))
    }
    return {"scopes": selected, "receipts": {identity: receipts[identity] for identity in wanted}}


def read_financial_input(
    store: ObjectStore, descriptor: dict[str, JsonValue], *, days: Sequence[date]
) -> dict[str, Any]:
    """Resolve a fixed selection, including old inline inputs, without latest/head/runs.

    Read only requested months. Receipt statuses are rebuilt from the original
    packs by the observation owner, not retained as another daily L2 copy.
    """
    if "format" not in descriptor:
        legacy_scopes, legacy_receipts = descriptor["scopes"], descriptor["receipts"]
        if not isinstance(legacy_scopes, dict) or not isinstance(legacy_receipts, dict):
            raise ValueError("saved financial input lacks its fixed selection")
        if any(day.isoformat() not in legacy_scopes for day in days):
            raise ValueError("requested interval is outside the saved financial selection")
        return _bounded_input(legacy_scopes, legacy_receipts, days)
    if descriptor["format"] != INPUT_FORMAT:
        raise ValueError("unsupported fixed financial input format")
    digest = descriptor.get("selection_sha256")
    if not isinstance(digest, str):
        raise ValueError("fixed financial input requires its selection digest")
    root = _Selection.model_validate(_read(store, _key("selections", digest), digest))
    if any(not root.start <= day <= root.end for day in days):
        raise ValueError("requested interval is outside the saved financial selection")
    scopes: dict[str, Any] = {day.isoformat(): {} for day in days}
    receipts: dict[str, Any] = {}
    for month in sorted({day.strftime("%Y-%m") for day in days} & root.months.keys()):
        digest = root.months[month]
        block = _read(store, _key("months", digest), digest)
        if block.get("format") != "jquants-financial-month-v1" or block.get("month") != month:
            raise ValueError("fixed financial month differs from its selection")
        for day, scope in block["scopes"].items():
            if day[:7] != month or not root.start <= date.fromisoformat(day) <= root.end:
                raise ValueError("fixed financial day is outside its selection")
            if day in scopes:
                scopes[day] = scope
        receipts.update(block["receipts"])
    return _bounded_input(scopes, receipts, days)
