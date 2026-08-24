from __future__ import annotations

import unittest
from datetime import date

from baibai_engine.foundation.coerce import (
    dedupe_strings,
    dict_sequence,
    float_or,
    int_or,
    mapping_or_empty,
    mapping_sequence,
    metric_map,
    optional_float,
    parse_iso_date,
    string_or_none,
    string_sequence,
)


class OptionalFloatTests(unittest.TestCase):
    def test_optional_float_with_int_returns_float(self) -> None:
        self.assertEqual(optional_float(3), 3.0)

    def test_optional_float_with_bool_returns_none(self) -> None:
        self.assertIsNone(optional_float(True))

    def test_optional_float_with_none_returns_none(self) -> None:
        self.assertIsNone(optional_float(None))

    def test_optional_float_with_numeric_string_returns_none(self) -> None:
        self.assertIsNone(optional_float("3.5"))

    def test_float_or_with_non_number_returns_default(self) -> None:
        self.assertEqual(float_or("x", -1.0), -1.0)

    def test_int_or_with_float_truncates(self) -> None:
        self.assertEqual(int_or(2.9, 0), 2)

    def test_int_or_with_bool_returns_default(self) -> None:
        self.assertEqual(int_or(True, 7), 7)


class ParseIsoDateTests(unittest.TestCase):
    def test_parse_iso_date_with_plain_date_parses(self) -> None:
        self.assertEqual(parse_iso_date("2026-06-08"), date(2026, 6, 8))

    def test_parse_iso_date_with_datetime_suffix_takes_date_prefix(self) -> None:
        self.assertEqual(parse_iso_date("2026-06-08T09:00:00+09:00"), date(2026, 6, 8))

    def test_parse_iso_date_with_compact_form_returns_none(self) -> None:
        # date.fromisoformat would accept "20260608"; records must use YYYY-MM-DD.
        self.assertIsNone(parse_iso_date("20260608"))

    def test_parse_iso_date_with_month_precision_returns_none(self) -> None:
        self.assertIsNone(parse_iso_date("2026-06"))

    def test_parse_iso_date_with_non_string_returns_none(self) -> None:
        self.assertIsNone(parse_iso_date(20260608))


class SequenceCoercionTests(unittest.TestCase):
    def test_string_sequence_with_string_input_returns_empty(self) -> None:
        self.assertEqual(string_sequence("abc"), ())

    def test_string_sequence_filters_non_strings(self) -> None:
        self.assertEqual(string_sequence(["a", 1, "b"]), ("a", "b"))

    def test_mapping_sequence_filters_non_mappings(self) -> None:
        self.assertEqual(mapping_sequence([{"a": 1}, "x", 2]), ({"a": 1},))

    def test_dict_sequence_copies_mappings(self) -> None:
        source = {"a": 1}
        (copied,) = dict_sequence([source])
        self.assertEqual(copied, source)
        self.assertIsNot(copied, source)

    def test_dedupe_strings_keeps_first_occurrence_order(self) -> None:
        self.assertEqual(dedupe_strings(["b", "a", "b", "c", "a"]), ["b", "a", "c"])


class MappingCoercionTests(unittest.TestCase):
    def test_mapping_or_empty_with_non_mapping_returns_empty(self) -> None:
        self.assertEqual(mapping_or_empty(["a"]), {})

    def test_metric_map_copies_mapping(self) -> None:
        self.assertEqual(metric_map({"pbr": 0.8}), {"pbr": 0.8})

    def test_string_or_none_with_non_string_returns_none(self) -> None:
        self.assertIsNone(string_or_none(1))


if __name__ == "__main__":
    unittest.main()
