from __future__ import annotations

import unittest

from baibai_engine.screening.config import DEFAULT_CACHE_DIR, ScreeningConfig


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

    def test_local_configuration_does_not_require_provider_credentials(self) -> None:
        config = ScreeningConfig.from_env({})
        self.assertIsNone(config.jquants_api_key)
        self.assertIsNone(config.edinet_api_key)

    def test_jquants_network_boundary_requires_its_own_key(self) -> None:
        from baibai_engine.collection.jquants.http import JQuantsAcquisitionError, JQuantsHTTP

        with self.assertRaisesRegex(JQuantsAcquisitionError, "JQUANTS_API_KEY"):
            JQuantsHTTP("")


def test_cache_only_run_reaches_local_preflight_without_api_keys(tmp_path, monkeypatch, capsys):
    from baibai_engine.screening.cli import app

    # Do not let a developer or repository .env reintroduce credentials.
    monkeypatch.setattr(app, "load_project_env", lambda: None)

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("JQUANTS_API_KEY", raising=False)
    monkeypatch.delenv("EDINET_API_KEY", raising=False)
    # The store is absent: fail for the missing local input, not unrelated credentials.
    assert app.main(["run", "--asof", "2026-04-24"]) == 1
    error = capsys.readouterr().err
    assert "SQLite cache coverage incomplete" in error
    assert "JQUANTS_API_KEY" not in error
