"""ClientV2 transport shapes must not manufacture partial source records."""

from unittest.mock import Mock

import pytest

from baibai_engine.market.jquants_decode import JQuantsProviderError, _payload_to_records


def test_record_rows_preserve_native_fields_and_missing_values():
    rows = [{"Code": "13010", "C": None, "AdjFactor": 0.0, "unknown_field": ""}]
    assert _payload_to_records(rows) == rows
    assert _payload_to_records([]) == []


def test_dataframe_conversion_uses_records_once():
    frame = Mock()
    frame.to_dict.return_value = [{"Code": "13010", "C": 100.0}]
    assert _payload_to_records(frame) == frame.to_dict.return_value
    frame.to_dict.assert_called_once_with(orient="records")


@pytest.mark.parametrize(
    "payload",
    [None, "bad", {"daily_quotes": []}, {"Code": "13010"}, [{"Code": "13010"}, 42]],
)
def test_malformed_shapes_are_not_empty_or_partial_success(payload):
    with pytest.raises(JQuantsProviderError):
        _payload_to_records(payload)


def test_dataframe_type_error_does_not_retry_a_different_orientation():
    frame = Mock()
    frame.to_dict.side_effect = TypeError("bad record conversion")
    with pytest.raises(TypeError, match="bad record conversion"):
        _payload_to_records(frame)
    frame.to_dict.assert_called_once_with(orient="records")
