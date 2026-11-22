"""Rehydrate an exact native input without exposing its checked descriptor to mutation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, TypedDict

from pydantic import JsonValue

from baibai_engine.machine_analysis.generation.contracts import InputSnapshot

type _Scalar = str | int | float | bool | None
type _FrozenJson = _Scalar | Mapping[str, _FrozenJson] | tuple[_FrozenJson, ...]


class _SavedNativeInput(TypedDict):
    kind: Literal["native_l1"]
    reference: str
    sha256: str
    descriptor: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class VerifiedNativeInput:
    reference: str
    sha256: str
    descriptor: Mapping[str, _FrozenJson]

    def as_saved_record(self) -> _SavedNativeInput:
        """Return detached JSON-compatible data in the persisted InputSnapshot format."""
        return {
            "kind": "native_l1",
            "reference": self.reference,
            "sha256": self.sha256,
            "descriptor": {key: _saved_json(value) for key, value in self.descriptor.items()},
        }


def rehydrate_native_input(saved: InputSnapshot | bytes, *, source: str) -> VerifiedNativeInput:
    """Check the shape and digest once, before a reader uses any locator."""
    # Revalidate live values: frozen InputSnapshot has mutable JSON children, and
    # model_copy/update can bypass validation. Do not serialize it back to JSON.
    parsed = (
        InputSnapshot.model_validate_json(saved)
        if isinstance(saved, bytes)
        else InputSnapshot.model_validate(vars(saved))
    )
    descriptor = parsed.descriptor
    if parsed.kind != "native_l1" or descriptor is None:
        raise ValueError(f"a digest-verified native {source} input is required")
    return VerifiedNativeInput(
        parsed.reference,
        parsed.sha256,
        MappingProxyType({key: _frozen_json(value) for key, value in descriptor.items()}),
    )


def _frozen_json(value: JsonValue) -> _FrozenJson:
    if isinstance(value, dict):
        return MappingProxyType({key: _frozen_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_frozen_json(item) for item in value)
    return value


def _saved_json(value: _FrozenJson) -> JsonValue:
    if isinstance(value, Mapping):
        return {key: _saved_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_saved_json(item) for item in value]
    return value
