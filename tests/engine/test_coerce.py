from __future__ import annotations

import unittest

from baibai_engine.foundation.coerce import metric_map, optional_float, string_or_none


class OptionalFloatTests(unittest.TestCase):
    def test_optional_float_with_int_returns_float(self) -> None:
        self.assertEqual(optional_float(3), 3.0)

    def test_optional_float_with_bool_returns_none(self) -> None:
        self.assertIsNone(optional_float(True))

    def test_optional_float_with_none_returns_none(self) -> None:
        self.assertIsNone(optional_float(None))

    def test_optional_float_with_numeric_string_returns_none(self) -> None:
        self.assertIsNone(optional_float("3.5"))


class MappingCoercionTests(unittest.TestCase):
    def test_metric_map_copies_mapping(self) -> None:
        self.assertEqual(metric_map({"pbr": 0.8}), {"pbr": 0.8})

    def test_string_or_none_with_non_string_returns_none(self) -> None:
        self.assertIsNone(string_or_none(1))


if __name__ == "__main__":
    unittest.main()
