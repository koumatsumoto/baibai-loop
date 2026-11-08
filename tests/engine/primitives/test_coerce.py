from __future__ import annotations

import unittest

from baibai_engine.primitives.coerce import optional_float, string_or_none


class OptionalFloatTests(unittest.TestCase):
    def test_optional_float_with_int_returns_float(self) -> None:
        self.assertEqual(optional_float(3), 3.0)

    def test_optional_float_with_bool_returns_none(self) -> None:
        self.assertIsNone(optional_float(True))

    def test_optional_float_with_none_returns_none(self) -> None:
        self.assertIsNone(optional_float(None))

    def test_optional_float_with_numeric_string_returns_none(self) -> None:
        self.assertIsNone(optional_float("3.5"))


class StringCoercionTests(unittest.TestCase):
    def test_string_or_none_with_non_string_returns_none(self) -> None:
        self.assertIsNone(string_or_none(1))


if __name__ == "__main__":
    unittest.main()
