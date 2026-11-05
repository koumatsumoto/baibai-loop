"""Share immutable monthly full-day input metadata without copying source bodies."""

from __future__ import annotations

import base64
import hashlib
import json
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, JsonValue, model_validator

from baibai_engine.machine_analysis.generation.contracts import Digest
from baibai_engine.primitives.json import canonical_json
from baibai_engine.storage.objects.r2 import LakePublishError, ObjectStore


@dataclass(frozen=True, slots=True)
class FullDayInputContract:
    prefix: str
    input_format: str
    selection_format: str
    month_format: str
    allow_legacy_inline: bool = False


class _Selection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    format: str
    start: date
    end: date
    months: dict[str, Digest]

    @model_validator(mode="after")
    def bounded_months(self) -> _Selection:
        if self.start > self.end:
            raise ValueError("invalid fixed full-day selection interval")
        for month in self.months:
            first = date.fromisoformat(month + "-01")
            if first.strftime("%Y-%m") != month or not (
                self.start.strftime("%Y-%m") <= month <= self.end.strftime("%Y-%m")
            ):
                raise ValueError("full-day month is outside its selection")
        return self


def _key(kind: str, digest: str, contract: FullDayInputContract) -> str:
    # Validate saved locators before composing a key; never accept caller paths.
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("invalid fixed full-day object digest")
    return contract.prefix + kind + "/" + digest + ".json"


def _read(store: ObjectStore, key: str, digest: str) -> dict[str, Any]:
    if store.head(key) is None:
        raise LakePublishError("fixed full-day input object is unavailable")
    payload = store.get_bytes(key)
    if hashlib.sha256(payload).hexdigest() != digest:
        raise LakePublishError("fixed full-day input object failed integrity")
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("fixed full-day input object must be an object")
    return value


def _retain(
    store: ObjectStore,
    kind: str,
    value: dict[str, Any],
    directory: Path,
    contract: FullDayInputContract,
) -> str:
    payload = canonical_json(value).encode()
    digest = hashlib.sha256(payload).hexdigest()
    key = _key(kind, digest, contract)
    existing = store.head(key)
    if existing is not None:
        if existing.size != len(payload) or existing.metadata.get("sha256") != digest:
            raise LakePublishError("fixed full-day input object identity conflicts")
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


def retain_full_day_input(
    store: ObjectStore, selected: dict[str, Any], *, contract: FullDayInputContract
) -> dict[str, JsonValue]:
    """Write only changed monthly metadata; the saved identity has bounded size.

    These objects are reconstruction inputs retained independently of runs cache.
    No latest pointer or automatic expiry is created here.
    """
    scopes, receipts = selected["scopes"], selected["receipts"]
    days = sorted(scopes)
    if not days:
        raise ValueError("full-day input selection requires its date interval")
    months: dict[str, dict[str, Any]] = {}
    for day in days:
        if not (scope := scopes[day]):
            continue  # The root's closed interval fixes missing days explicitly.
        month = day[:7]
        block = months.setdefault(
            month,
            {"format": contract.month_format, "month": month, "scopes": {}, "receipts": {}},
        )
        block["scopes"][day] = scope
        for field in ("adopted", "failure", "held"):
            if identity := scope.get(field):
                block["receipts"][identity] = receipts[identity]
    with tempfile.TemporaryDirectory(prefix=".full-day-input-") as temporary:
        directory = Path(temporary)
        identities = {
            month: _retain(store, "months", block, directory, contract)
            for month, block in months.items()
        }
        root = _Selection(
            format=contract.selection_format, start=days[0], end=days[-1], months=identities
        )
        digest = _retain(store, "selections", root.model_dump(mode="json"), directory, contract)
    return {
        "format": contract.input_format,
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


def read_full_day_input(
    store: ObjectStore,
    descriptor: dict[str, JsonValue],
    *,
    days: Sequence[date],
    contract: FullDayInputContract,
) -> dict[str, Any]:
    """Resolve a fixed selection, including old inline inputs, without latest/head/runs.

    Read only requested months. Receipt statuses are rebuilt from the original
    packs by the observation owner, not retained as another daily L2 copy.
    """
    if "format" not in descriptor and contract.allow_legacy_inline:
        legacy_scopes, legacy_receipts = descriptor["scopes"], descriptor["receipts"]
        if not isinstance(legacy_scopes, dict) or not isinstance(legacy_receipts, dict):
            raise ValueError("saved full-day input lacks its fixed selection")
        if any(day.isoformat() not in legacy_scopes for day in days):
            raise ValueError("requested interval is outside the saved full-day selection")
        return _bounded_input(legacy_scopes, legacy_receipts, days)
    if descriptor.get("format") != contract.input_format:
        raise ValueError("unsupported fixed full-day input format")
    digest = descriptor.get("selection_sha256")
    if not isinstance(digest, str):
        raise ValueError("fixed full-day input requires its selection digest")
    root = _Selection.model_validate(_read(store, _key("selections", digest, contract), digest))
    if root.format != contract.selection_format:
        raise ValueError("unsupported fixed selection format")
    if any(not root.start <= day <= root.end for day in days):
        raise ValueError("requested interval is outside the saved full-day selection")
    scopes: dict[str, Any] = {day.isoformat(): {} for day in days}
    receipts: dict[str, Any] = {}
    for month in sorted({day.strftime("%Y-%m") for day in days} & root.months.keys()):
        digest = root.months[month]
        block = _read(store, _key("months", digest, contract), digest)
        if block.get("format") != contract.month_format or block.get("month") != month:
            raise ValueError("fixed full-day month differs from its selection")
        for day, scope in block["scopes"].items():
            if day[:7] != month or not root.start <= date.fromisoformat(day) <= root.end:
                raise ValueError("fixed full-day day is outside its selection")
            if day in scopes:
                scopes[day] = scope
        receipts.update(block["receipts"])
    return _bounded_input(scopes, receipts, days)
