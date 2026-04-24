from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_loop.screening.config import ConfigError, DEFAULT_CACHE_DIR, ScreeningConfig


class ScreeningConfigTests(unittest.TestCase):
    def test_from_env_uses_default_cache_dir(self) -> None:
        config = ScreeningConfig.from_env(
            {
                "JQUANTS_REFRESH_TOKEN": "token",
                "EDINET_API_KEY": "key",
            }
        )

        self.assertEqual(config.cache_dir, DEFAULT_CACHE_DIR)

    def test_from_env_supports_custom_cache_dir(self) -> None:
        config = ScreeningConfig.from_env(
            {
                "JQUANTS_REFRESH_TOKEN": "token",
                "EDINET_API_KEY": "key",
                "SCREENING_CACHE_DIR": "/tmp/cache",
            }
        )

        self.assertEqual(config.cache_dir, Path("/tmp/cache"))

    def test_from_env_requires_tokens(self) -> None:
        with self.assertRaises(ConfigError):
            ScreeningConfig.from_env({})
