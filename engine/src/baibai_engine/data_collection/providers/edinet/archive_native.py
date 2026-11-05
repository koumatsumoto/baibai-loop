"""EDINETの初回保存ZIPと取得scopeを、market公開とは独立して保持する。"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from baibai_engine.data_collection.providers.edinet.facts_scope import FactsScope
from baibai_engine.data_collection.providers.edinet.fields import parse_doc_id
from baibai_engine.data_collection.providers.edinet.models import EdinetResponse
from baibai_engine.storage.filesystem import write_bytes_atomic
from baibai_engine.storage.json import canonical_json, write_json_atomic
from baibai_engine.storage.objects.r2 import LakeCASConflict, LakePublishError, ObjectStore

PREFIX = "l1/edinet/archives/v1/"
HEAD_KEY = PREFIX + "head.json"
_FORMAT: Final = "edinet-archive-native-v1"


def _digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _body_key(digest: str) -> str:
    return PREFIX + "objects/" + digest + ".zip"


class ArchiveMetadata(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    doc_id: str
    source_type: Literal[1, 5]
    stored_on: date
    received_at_utc: datetime | None = None
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)

    @field_validator("source_type", mode="before")
    @classmethod
    def source_type_is_integer(cls, value: Any) -> Any:
        if type(value) is not int:
            raise ValueError("EDINET archive type must be an integer")
        return value

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if parse_doc_id(self.doc_id) != self.doc_id:
            raise ValueError("EDINET archive requires a normalized docID")
        if self.received_at_utc is not None and (
            self.received_at_utc.utcoffset() != timedelta(0)
            or self.received_at_utc.date() != self.stored_on
        ):
            raise ValueError("EDINET archive receipt must match its UTC storage day")
        return self

    @property
    def key(self) -> tuple[str, int]:
        return self.doc_id, self.source_type


@dataclass(frozen=True, slots=True)
class NativeArchive:
    metadata: ArchiveMetadata
    content: bytes

    def __post_init__(self) -> None:
        if _digest(self.content) != self.metadata.sha256 or len(self.content) != self.metadata.size:
            raise ValueError("EDINET archive bytes differ from metadata")

    @classmethod
    def from_response(
        cls, doc_id: str, source_type: Literal[1, 5], response: EdinetResponse
    ) -> Self:
        return cls(
            ArchiveMetadata(
                doc_id=doc_id,
                source_type=source_type,
                stored_on=response.received_at.astimezone(UTC).date(),
                received_at_utc=response.received_at,
                sha256=_digest(response.body),
                size=len(response.body),
            ),
            response.body,
        )


class _Scope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    since: date | None
    doc_ids: tuple[str, ...]
    legacy_origins: tuple[str, ...]

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        if any(not key for key in self.doc_ids) or any(
            not re.fullmatch(r"[0-9a-f]{64}", key) for key in self.legacy_origins
        ):
            raise ValueError("invalid EDINET acquisition scope")
        return self

    @classmethod
    def from_scope(cls, scope: FactsScope) -> Self:
        return cls(
            since=scope.since,
            doc_ids=tuple(sorted(scope.doc_ids)),
            legacy_origins=tuple(sorted(scope.legacy_origins)),
        )

    def scope(self) -> FactsScope:
        return FactsScope(self.since, frozenset(self.doc_ids), frozenset(self.legacy_origins))


class _Head(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    format: Literal["edinet-archive-native-v1"]
    archives: tuple[ArchiveMetadata, ...]
    # 取得範囲は原本でも抽出成功履歴でもない。同じownerの別状態として保持する。
    acquisition_scope: _Scope

    @model_validator(mode="after")
    def unique_archives(self) -> Self:
        if len({item.key for item in self.archives}) != len(self.archives):
            raise ValueError("duplicate EDINET archive identity")
        return self

    def body(self) -> bytes:
        return canonical_json(self.model_dump(mode="json"))


def _read(store: ObjectStore, key: str) -> tuple[bytes, str] | None:
    before = store.head(key)
    if before is None:
        return None
    body = store.get_bytes(key)
    after = store.head(key)
    if (
        before != after
        or len(body) != before.size
        or _digest(body) != before.metadata.get("sha256")
    ):
        raise LakePublishError("EDINET archive object failed integrity: " + key)
    return body, before.etag


class FixedEdinetArchives:
    def __init__(self, store: ObjectStore, head: dict[str, Any] | bytes) -> None:
        self._store = store
        self._head = _Head.model_validate_json(
            head if isinstance(head, bytes) else canonical_json(head)
        )
        self._entries = {item.key: item for item in self._head.archives}

    @property
    def identity(self) -> str:
        return _digest(self._head.body())

    @property
    def scope(self) -> FactsScope:
        return self._head.acquisition_scope.scope()

    @property
    def archive_keys(self) -> frozenset[tuple[str, int]]:
        return frozenset(self._entries)

    def value(self) -> dict[str, Any]:
        return self._head.model_dump(mode="json")

    def metadata(self, doc_id: str, source_type: int) -> ArchiveMetadata | None:
        if (
            parse_doc_id(doc_id) != doc_id
            or type(source_type) is not int
            or source_type not in (1, 5)
        ):
            raise ValueError("invalid EDINET archive lookup identity")
        return self._entries.get((doc_id, source_type))

    def read_archive(self, doc_id: str, source_type: int) -> bytes:
        entry = self.metadata(doc_id, source_type)
        if entry is None:
            raise ValueError("retained EDINET archive is unavailable")
        saved = _read(self._store, _body_key(entry.sha256))
        if saved is None or _digest(saved[0]) != entry.sha256 or len(saved[0]) != entry.size:
            raise LakePublishError("retained EDINET archive is missing or changed")
        return saved[0]

    def read_csv_zip(self, doc_id: str) -> bytes:
        return self.read_archive(doc_id, 5)

    def read_xbrl_zip(self, doc_id: str) -> bytes:
        return self.read_archive(doc_id, 1)


class EdinetArchiveL1:
    def __init__(self, store: ObjectStore, directory: Path) -> None:
        self.store, self.directory = store, directory

    def require_clean_work(self) -> None:
        if (self.directory / "pending.json").exists():
            raise LakePublishError(
                "EDINET unpublished archives exist; call resume_work before HTTP"
            )

    def freeze(self) -> FixedEdinetArchives:
        saved = _read(self.store, HEAD_KEY)
        if saved is None:
            raise LakePublishError(
                "EDINET archive head is missing; explicit initialization required"
            )
        return FixedEdinetArchives(self.store, saved[0])

    def initialize(self) -> FixedEdinetArchives:
        self.require_clean_work()
        if self.store.head(HEAD_KEY) is not None:
            raise FileExistsError("EDINET archive head already exists")
        return self._start((), FactsScope(), create=True)

    def publish(
        self, archives: Sequence[NativeArchive], *, scope: FactsScope | None = None
    ) -> FixedEdinetArchives:
        self.require_clean_work()
        self.freeze()
        return self._start(archives, scope or FactsScope(), create=False)

    def _start(
        self, archives: Sequence[NativeArchive], scope: FactsScope, *, create: bool
    ) -> FixedEdinetArchives:
        additions = _Head(
            format=_FORMAT,
            archives=tuple(item.metadata for item in archives),
            acquisition_scope=_Scope.from_scope(scope),
        )
        self.directory.mkdir(parents=True, exist_ok=True)
        for item in archives:
            write_bytes_atomic(self.directory / (item.metadata.sha256 + ".object"), item.content)
        write_json_atomic(
            self.directory / "pending.json",
            {"create": create, "head": additions.model_dump(mode="json")},
        )
        return self.resume_work()

    def _put(self, key: str, body: bytes, *, etag: str | None) -> None:
        path = self.directory / (_digest(body) + ".object")
        write_bytes_atomic(path, body)
        try:
            self.store.put_file(
                key,
                path,
                sha256=_digest(body),
                content_md5=base64.b64encode(
                    hashlib.md5(body, usedforsecurity=False).digest()
                ).decode(),
                content_type="application/json" if key == HEAD_KEY else "application/zip",
                if_match=etag,
                if_none_match=etag is None,
            )
        except LakeCASConflict:
            raise
        except LakePublishError:
            saved = _read(self.store, key)
            if saved is None or saved[0] != body:
                raise
        saved = _read(self.store, key)
        if saved is None or saved[0] != body:
            raise LakePublishError("EDINET archive write outcome is unconfirmed")

    def resume_work(self) -> FixedEdinetArchives:
        pending_path = self.directory / "pending.json"
        pending = json.loads(pending_path.read_bytes())
        if set(pending) != {"create", "head"} or type(pending["create"]) is not bool:
            raise ValueError("invalid EDINET archive pending work")
        incoming = _Head.model_validate_json(canonical_json(pending["head"]))
        saved = _read(self.store, HEAD_KEY)
        if saved is None and not pending["create"]:
            raise LakePublishError(
                "EDINET archive head disappeared; do not initialize from pending work"
            )
        current = (
            _Head.model_validate_json(saved[0])
            if saved
            else _Head(
                format=_FORMAT, archives=(), acquisition_scope=_Scope.from_scope(FactsScope())
            )
        )
        if pending["create"] and current != incoming:
            raise LakeCASConflict("EDINET archive initialization conflicts with an existing head")
        entries = {item.key: item for item in current.archives}
        for item in incoming.archives:
            body = (self.directory / (item.sha256 + ".object")).read_bytes()
            NativeArchive(item, body)
            previous = entries.get(item.key)
            if previous is not None and previous.sha256 != item.sha256:
                raise ValueError("EDINET archive identity has conflicting bytes")
            # 同値の後着は初回保存日を変えない。本文だけはidentity間でも共有する。
            entries.setdefault(item.key, item)
            key = _body_key(item.sha256)
            existing = _read(self.store, key)
            if existing is None:
                self._put(key, body, etag=None)
            elif existing[0] != body:
                raise LakePublishError("EDINET immutable archive hash mismatch")
        candidate = _Head(
            format=_FORMAT,
            archives=tuple(entries[key] for key in sorted(entries)),
            acquisition_scope=_Scope.from_scope(
                current.acquisition_scope.scope().join(incoming.acquisition_scope.scope())
            ),
        )
        body = candidate.body()
        if saved is None or body != saved[0]:
            self._put(HEAD_KEY, body, etag=saved[1] if saved else None)
        pending_path.unlink()
        return FixedEdinetArchives(self.store, body)
