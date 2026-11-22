"""Exact native input identity is rechecked against detached immutable contents."""

import hashlib

import pytest

from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.machine_analysis.generation.native_input import rehydrate_native_input
from baibai_engine.primitives.json import canonical_json


def _snapshot() -> InputSnapshot:
    descriptor = {"summary": {"days": [{"状態": None}, 0, False]}}
    return InputSnapshot(
        kind="native_l1",
        reference="fixture",
        sha256=hashlib.sha256(canonical_json(descriptor).encode()).hexdigest(),
        descriptor=descriptor,
    )


@pytest.mark.parametrize("serialized", [False, True])
def test_verified_input_does_not_share_nested_values_with_either_raw_or_serialized_copy(serialized):
    saved = _snapshot()
    original = saved.model_dump(mode="json")
    verified = rehydrate_native_input(
        saved.model_dump_json().encode() if serialized else saved, source="fixture"
    )
    saved.descriptor["summary"]["days"][0]["状態"] = "mutated"
    with pytest.raises(TypeError):
        verified.descriptor["summary"]["days"][0]["状態"] = "mutated"
    output = verified.as_saved_record()
    assert output == original
    output["descriptor"]["summary"]["days"][0]["状態"] = "mutated"
    assert verified.as_saved_record() == original


def test_live_nonfinite_tampering_cannot_serialize_back_into_matching_null():
    saved = _snapshot()
    saved.descriptor["summary"]["days"][0]["状態"] = float("nan")
    with pytest.raises(ValueError, match="Out of range float"):
        rehydrate_native_input(saved, source="fixture")
