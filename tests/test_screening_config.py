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

    def test_from_env_collects_jpx_regulation_urls(self) -> None:
        config = ScreeningConfig.from_env(
            {
                "JQUANTS_REFRESH_TOKEN": "token",
                "EDINET_API_KEY": "key",
                "JPX_SPECIAL_CAUTION_URL": "https://example.com/special.csv",
                "JPX_TRADING_HALT_URL": "https://example.com/halt.csv",
            }
        )

        self.assertEqual(
            dict(config.jpx_regulation_urls),
            {
                "特別注意銘柄": "https://example.com/special.csv",
                "取引停止": "https://example.com/halt.csv",
            },
        )

    def test_from_env_requires_tokens(self) -> None:
        with self.assertRaises(ConfigError):
            ScreeningConfig.from_env({})
