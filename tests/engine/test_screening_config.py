from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_engine.screening.config import DEFAULT_CACHE_DIR, ConfigError, ScreeningConfig


class ScreeningConfigTests(unittest.TestCase):
    def test_from_env_uses_default_cache_dir(self) -> None:
        config = ScreeningConfig.from_env(
            {
                "JQUANTS_API_KEY": "token",
                "EDINET_API_KEY": "key",
            }
        )

        self.assertEqual(config.cache_dir, DEFAULT_CACHE_DIR)

    def test_from_env_ignores_screening_cache_dir_override(self) -> None:
        # SCREENING_CACHE_DIR の env override は廃止 (config.py の意図的な
        # hardcode 化、stale .env が canonical を見失う regression を防ぐ)。
        config = ScreeningConfig.from_env(
            {
                "JQUANTS_API_KEY": "token",
                "EDINET_API_KEY": "key",
                "SCREENING_CACHE_DIR": "/tmp/cache",
            }
        )

        self.assertEqual(config.cache_dir, DEFAULT_CACHE_DIR)

    def test_from_env_collects_jpx_regulation_urls(self) -> None:
        config = ScreeningConfig.from_env(
            {
                "JQUANTS_API_KEY": "token",
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

    def test_from_env_supports_special_caution_index_url(self) -> None:
        config = ScreeningConfig.from_env(
            {
                "JQUANTS_API_KEY": "token",
                "EDINET_API_KEY": "key",
                "JPX_SPECIAL_CAUTION_INDEX_URL": "https://www.jpx.co.jp/markets/statistics-equities/margin/index.html",
            }
        )

        self.assertEqual(
            config.jpx_special_caution_index_url,
            "https://www.jpx.co.jp/markets/statistics-equities/margin/index.html",
        )
        self.assertEqual(
            dict(config.jpx_regulation_urls),
            {
                "特別注意銘柄": "https://www.jpx.co.jp/markets/statistics-equities/margin/index.html",
            },
        )

    def test_from_env_requires_tokens(self) -> None:
        with self.assertRaises(ConfigError):
            ScreeningConfig.from_env({})
