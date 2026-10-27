from __future__ import annotations

import unittest

from baibai_engine.foundation.repository_layout import MARKET_DB_PATH
from baibai_engine.source_maintenance.config import SourceMaintenanceConfig


class SourceMaintenanceConfigTests(unittest.TestCase):
    def test_from_env_uses_canonical_market_store_directory(self) -> None:
        config = SourceMaintenanceConfig.from_env(
            {
                "JQUANTS_API_KEY": "token",
                "EDINET_API_KEY": "key",
            }
        )

        self.assertEqual(config.sqlite_cache_dir, MARKET_DB_PATH.parent)
        self.assertEqual(config.jquants_api_key, "token")
        self.assertEqual(config.edinet_api_key, "key")

    def test_local_configuration_does_not_require_provider_credentials(self) -> None:
        config = SourceMaintenanceConfig.from_env({})
        self.assertIsNone(config.jquants_api_key)
        self.assertIsNone(config.edinet_api_key)

    def test_jquants_network_boundary_requires_its_own_key(self) -> None:
        from baibai_engine.data_collection.jquants.http import JQuantsAcquisitionError, JQuantsHTTP

        with self.assertRaisesRegex(JQuantsAcquisitionError, "JQUANTS_API_KEY"):
            JQuantsHTTP("")
