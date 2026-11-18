"""Self-contained fixed economic receipt membership, without retaining any native payload."""

from __future__ import annotations

import base64
import hashlib
import lzma
from collections.abc import Iterator
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from baibai_engine.machine_analysis.generation.contracts import Digest
from baibai_engine.primitives.json import encode_json

if TYPE_CHECKING:
    from baibai_engine.machine_analysis.generation.economic_inputs import _Receipt

# Cap expanded bytes separately from the private object's existing 2 MB wire limit.
_MAX_EXPANDED = 24 * 1024**2
type _Kind = Literal["records", "documents"]
type _State = tuple[str, bool, str | None]
type _Group = tuple[_Kind, str, list[_State], list[tuple[str, Annotated[int, Field(ge=0)]]]]
type Member = tuple[str, tuple[str, ...], bool, str | None, str | None]


class _Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: Literal["economic-membership-xz-v1"]
    data: str


class PackedMembership(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selection: dict[str, Any]
    receipt_sha256: Digest
    groups: list[_Group]

    def members(self) -> Iterator[Member]:
        for kind, source, states, keys in self.groups:
            for request_key, index in keys:
                if index >= len(states):
                    raise ValueError("invalid fixed economic receipt state")
                fetched, confirmed, checked = states[index]
                yield kind, (source, request_key, fetched), confirmed, checked, None


def encode_membership(
    legacy: dict[str, Any], blocks: dict[str, dict[str, list[_Receipt]]]
) -> dict[str, str] | None:
    # The authenticated root holds all row identities and clocks in order. The
    # concatenation of fixed-length native row digests binds every row in that order.
    proof = hashlib.sha256()
    groups: list[_Group] = []
    for name in sorted(blocks):
        for kind in ("records", "documents"):
            rows = blocks[name][kind]
            if not rows:
                continue
            states: dict[_State, int] = {}
            keys = []
            for row in rows:
                state = (row.key[2], row.confirmed, row.checked_at)
                keys.append((row.key[1], states.setdefault(state, len(states))))
                proof.update(bytes.fromhex(row.sha256))
            groups.append((kind, rows[0].key[0], list(states), keys))
    header = dict(legacy)
    header["months"] = {}
    body = encode_json(
        {
            "selection": header,
            "receipt_sha256": proof.hexdigest(),
            "groups": groups,
        }
    ).encode()
    if len(body) > _MAX_EXPANDED:
        return None
    return {
        "format": "economic-membership-xz-v1",
        "data": base64.b64encode(lzma.compress(body, preset=6)).decode("ascii"),
    }


def decode_membership(payload: bytes) -> PackedMembership:
    envelope = _Envelope.model_validate_json(payload)
    decoder = lzma.LZMADecompressor(format=lzma.FORMAT_XZ, memlimit=32 * 1024**2)
    body = decoder.decompress(base64.b64decode(envelope.data, validate=True), _MAX_EXPANDED + 1)
    if len(body) > _MAX_EXPANDED or not decoder.eof or decoder.unused_data:
        raise ValueError("invalid fixed economic membership envelope")
    return PackedMembership.model_validate_json(body)
