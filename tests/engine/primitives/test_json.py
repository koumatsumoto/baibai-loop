"""Canonical identity serialization is independent of any task or judgment domain."""

import json
from datetime import date, datetime
from decimal import Decimal

import pytest

from baibai_engine.primitives.json import canonical_json


def test_canonical_json_normalizes_supported_representations() -> None:
    value = {
        "date": date(2026, 7, 19),
        "datetime": datetime.fromisoformat("2026-07-19T12:00:00+09:00"),
        "decimal": Decimal("1.20"),
        "tuple": (1, None),
    }
    assert json.loads(canonical_json(value)) == {
        "date": "2026-07-19",
        "datetime": "2026-07-19T12:00:00+09:00",
        "decimal": "1.20",
        "tuple": [1, None],
    }


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_float_cannot_enter_canonical_identity(value):
    with pytest.raises(ValueError, match="Out of range float"):
        canonical_json({"nested": [value]})


def test_key_order_is_canonical_without_collapsing_null_zero_false_or_text():
    first = {"text": "0", "null": None, "zero": 0, "false": False}
    second = dict(reversed(list(first.items())))
    expected = '{"false":false,"null":null,"text":"0","zero":0}'
    assert canonical_json(first) == canonical_json(second) == expected


def test_unsupported_bytes_are_not_silently_decoded_or_stringified():
    with pytest.raises(TypeError, match="unsupported canonical JSON value: bytes"):
        canonical_json({"raw": b"source"})
